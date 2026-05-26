from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import yaml
from typer.testing import CliRunner

from harness import model_discovery
from harness.active_provider_registry import ActiveProviderRegistry
from harness.cli.main import app
from harness.config import default_config, load_config
from harness.memory.sqlite_store import SQLiteStore
from harness.model_catalog import ProviderCredentialStatus, list_model_catalog, list_provider_catalog, validate_model_selection
from harness.model_discovery import (
    AnthropicStaticCatalogDiscoveryAdapter,
    BedrockFoundationModelsDiscoveryAdapter,
    GoogleGenerativeDiscoveryAdapter,
    OpenAICompatibleDiscoveryAdapter,
    ProviderDiscoveryPolicy,
    build_default_provider_discovery_registry,
    list_cached_discovered_models,
    refresh_model_discovery,
)
from harness.model_registry import build_provider_descriptors
from harness.operator_context import build_tui_dashboard
from harness.provider_auth import (
    ProviderCredentialResolutionError,
    ResolvedProviderCredential,
    provider_oauth_callback,
    provider_secret_store_path,
    read_provider_account_secret,
    resolve_provider_credential,
    write_provider_oauth_tokens,
)


runner = CliRunner()


class FakeDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {"data": [{"id": "zeta-local"}, {"id": "alpha-local"}, {"id": "alpha-local"}]}

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("model discovery must not call chat completions")


class InvalidDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {"unexpected": []}

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("model discovery must not call chat completions")


class FakeGoogleDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                    "description": "Fast Gemini model.",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 8192,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/gemini-2.5-pro",
                    "displayName": "Gemini 2.5 Pro",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 8192,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {"name": "models/gemini-2.5-pro"},
            ]
        }

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("model discovery must not call content generation")


class FakeOpenRouterDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {
            "data": [
                {
                    "id": "anthropic/claude-3.7-sonnet",
                    "name": "Claude 3.7 Sonnet",
                    "description": "OpenRouter model metadata.",
                    "context_length": 200000,
                    "architecture": {
                        "modality": "text+image->text",
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "tokenizer": "Claude",
                    },
                    "pricing": {
                        "prompt": "0.000003",
                        "completion": "0.000015",
                    },
                    "top_provider": {
                        "context_length": 200000,
                        "max_completion_tokens": 8192,
                        "is_moderated": False,
                    },
                    "supported_parameters": ["tools", "tool_choice", "reasoning"],
                    "per_request_limits": {},
                }
            ]
        }

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("model discovery must not call chat completions")


class FakeBedrockDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {
            "modelSummaries": [
                {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-7-sonnet-20250219-v1:0",
                    "modelId": "anthropic.claude-3-7-sonnet-20250219-v1:0",
                    "modelName": "Claude 3.7 Sonnet",
                    "providerName": "Anthropic",
                    "inputModalities": ["TEXT", "IMAGE"],
                    "outputModalities": ["TEXT"],
                    "responseStreamingSupported": True,
                    "customizationsSupported": [],
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "modelLifecycle": {"status": "ACTIVE"},
                }
            ]
        }

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("model discovery must not call model execution")


def test_provider_discovery_adapter_registry_is_metadata_only_until_refresh() -> None:
    cfg = default_config()
    providers = {provider.provider_id: provider for provider in build_provider_descriptors(cfg)}
    client = FakeDiscoveryHttpClient()
    registry = build_default_provider_discovery_registry(http_client=client)

    supported = registry.supported_adapters(providers["local_openai_compatible"])
    google_supported = registry.supported_adapters(providers["google"])

    assert len(supported) == 1
    assert supported[0].provider_id == "openai_compatible_models"
    assert len(google_supported) == 1
    assert google_supported[0].provider_id == "google_generative_models"
    assert client.gets == []
    anthropic_supported = registry.supported_adapters(providers["anthropic"])
    assert len(anthropic_supported) == 1
    assert anthropic_supported[0].provider_id == "anthropic_static_catalog"


def test_openai_compatible_discovery_adapter_discovers_only_when_called() -> None:
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "local_openai_compatible")
    client = FakeDiscoveryHttpClient()
    adapter = OpenAICompatibleDiscoveryAdapter(http_client=client)

    result = adapter.discover(provider, None, ProviderDiscoveryPolicy())

    assert result.ok is True
    assert result.provider_id == "local_openai_compatible"
    assert result.model_count == 2
    assert [model.raw_model_ref for model in result.models] == [
        "local_openai_compatible/alpha-local",
        "local_openai_compatible/zeta-local",
    ]
    assert result.network_accessed is True
    assert result.credentials_included is False
    assert client.gets == [
        {
            "url": "http://localhost:11434/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]


def test_google_discovery_requires_hosted_approval_before_network() -> None:
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "google")
    client = FakeGoogleDiscoveryHttpClient()
    adapter = GoogleGenerativeDiscoveryAdapter(http_client=client)

    try:
        adapter.discover(provider, None, ProviderDiscoveryPolicy())
    except model_discovery.ModelDiscoveryError as exc:
        assert exc.blocked_reasons == ["hosted_discovery_approval_required"]
    else:
        raise AssertionError("google hosted discovery must require explicit approval")

    assert client.gets == []


def test_google_discovery_adapter_lists_models_after_explicit_refresh_approval() -> None:
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "google")
    client = FakeGoogleDiscoveryHttpClient()
    adapter = GoogleGenerativeDiscoveryAdapter(http_client=client)

    result = adapter.discover(provider, None, ProviderDiscoveryPolicy(approve_hosted=True, timeout_seconds=12))

    assert result.ok is True
    assert result.provider_id == "google"
    assert result.model_count == 2
    assert result.network_accessed is True
    assert result.credentials_included is False
    assert [model.raw_model_ref for model in result.models] == ["google/gemini-2.5-flash", "google/gemini-2.5-pro"]
    flash = result.models[0]
    assert flash.context_limit == 1048576
    assert flash.max_output_tokens == 8192
    assert flash.modalities == ["text", "image"]
    assert flash.reasoning_support == "tokens"
    assert flash.tool_support is True
    assert flash.discovery_metadata["supported_generation_methods"] == ["generateContent", "countTokens"]
    assert client.gets == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 12.0,
        }
    ]


def test_openai_compatible_custom_router_uses_models_discovery(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "router_local": {
                        "display_name": "Router Local",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {
                            "seed-model": {
                                "context_window": 32768,
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    providers = {provider.provider_id: provider for provider in build_provider_descriptors(cfg)}
    client = FakeDiscoveryHttpClient()
    registry = build_default_provider_discovery_registry(http_client=client)

    supported = registry.supported_adapters(providers["router_local"])
    result = refresh_model_discovery(cfg, "router_local", http_client=client)

    assert providers["router_local"].protocol_defaults["protocol"] == "openai_chat"
    assert len(supported) == 1
    assert supported[0].provider_id == "openai_compatible_models"
    assert result.ok is True
    assert result.provider_id == "router_local"
    assert [model.raw_model_ref for model in result.models] == ["router_local/alpha-local", "router_local/zeta-local"]
    assert client.gets == [
        {
            "url": "http://localhost:11434/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]


def test_openrouter_compatible_models_discovery_preserves_router_metadata(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "openrouter_team": {
                        "display_name": "OpenRouter Team",
                        "enabled": False,
                        "data_boundary": "external_router",
                        "base_url": "https://openrouter.ai/api/v1",
                        "protocol": "openai_chat",
                        "timeout_seconds": 45,
                        "credential": {"kind": "env", "env_var": "OPENROUTER_API_KEY"},
                        "compatibility": {"provider": "openrouter"},
                        "models": {
                            "seed-model": {
                                "context_window": 32768,
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    providers = {provider.provider_id: provider for provider in build_provider_descriptors(cfg)}
    client = FakeOpenRouterDiscoveryHttpClient()

    result = refresh_model_discovery(cfg, "openrouter_team", http_client=client, approve_hosted=True)

    assert providers["openrouter_team"].protocol_defaults["compatibility"] == {"provider": "openrouter"}
    assert result.ok is True
    assert result.provider_id == "openrouter_team"
    assert result.model_count == 1
    model = result.models[0]
    assert model.raw_model_ref == "openrouter_team/anthropic/claude-3.7-sonnet"
    assert model.context_limit == 200000
    assert model.max_output_tokens == 8192
    assert model.modalities == ["image", "text"]
    assert model.reasoning_support == "effort"
    assert model.tool_support is True
    assert model.cost == {"prompt": "0.000003", "completion": "0.000015"}
    assert model.status == "active"
    assert model.discovery_metadata["compatibility"] == "openrouter"
    assert model.discovery_metadata["supported_parameters"] == ["tools", "tool_choice", "reasoning"]
    assert model.discovery_metadata["architecture"]["modality"] == "text+image->text"
    assert client.gets == [
        {
            "url": "https://openrouter.ai/api/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 45.0,
        }
    ]


def test_bedrock_foundation_model_discovery_requires_hosted_approval_before_network() -> None:
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "bedrock")
    client = FakeBedrockDiscoveryHttpClient()
    credential = ResolvedProviderCredential(
        provider_id="bedrock",
        credential_kind="aws_env",
        status="configured",
        source="aws_env",
        env_var="AWS_ACCESS_KEY_ID",
    )
    adapter = BedrockFoundationModelsDiscoveryAdapter(http_client=client)

    try:
        adapter.discover(provider, credential, ProviderDiscoveryPolicy(with_credentials=True))
    except model_discovery.ModelDiscoveryError as exc:
        assert exc.blocked_reasons == ["hosted_discovery_approval_required"]
    else:
        raise AssertionError("bedrock hosted discovery must require explicit approval")

    assert client.gets == []


def test_bedrock_foundation_model_discovery_lists_signed_metadata(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADISCOVERY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "bedrock-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "bedrock-session")
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "bedrock")
    client = FakeBedrockDiscoveryHttpClient()
    credential = ResolvedProviderCredential(
        provider_id="bedrock",
        credential_kind="aws_env",
        status="configured",
        source="aws_env",
        env_var="AWS_ACCESS_KEY_ID",
    )
    adapter = BedrockFoundationModelsDiscoveryAdapter(http_client=client)

    result = adapter.discover(
        provider,
        credential,
        ProviderDiscoveryPolicy(approve_hosted=True, with_credentials=True, timeout_seconds=17),
    )

    assert result.ok is True
    assert result.provider_id == "bedrock"
    assert result.model_count == 1
    assert result.credentials_included is True
    model = result.models[0]
    assert model.raw_model_ref == "bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0"
    assert model.endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert model.status == "active"
    assert model.modalities == ["image", "text"]
    assert model.tool_support is True
    assert model.discovery_metadata["endpoint"] == "https://bedrock.us-east-1.amazonaws.com/foundation-models"
    assert model.discovery_metadata["runtime_endpoint"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert model.discovery_metadata["aws_region"] == "us-east-1"
    assert model.discovery_metadata["model_arn"].endswith("anthropic.claude-3-7-sonnet-20250219-v1:0")
    assert model.discovery_metadata["response_streaming_supported"] is True
    assert model.discovery_metadata["inference_types_supported"] == ["on_demand"]
    assert client.gets[0]["url"] == "https://bedrock.us-east-1.amazonaws.com/foundation-models"
    assert client.gets[0]["timeout"] == 17.0
    headers = client.gets[0]["headers"]
    assert headers["Host"] == "bedrock.us-east-1.amazonaws.com"
    assert headers["X-Harness-AWS-Credential-Source"] == "aws_env"
    assert headers["X-Harness-AWS-Region"] == "us-east-1"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIADISCOVERY/")
    assert headers["X-Amz-Security-Token"] == "bedrock-session"
    assert "bedrock-secret" not in json.dumps(result.model_dump(mode="json"))
    assert "bedrock-secret" not in json.dumps(client.gets)


def test_anthropic_static_discovery_lists_generated_catalog_without_network() -> None:
    cfg = default_config()
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "anthropic")
    client = FakeDiscoveryHttpClient()
    adapter = AnthropicStaticCatalogDiscoveryAdapter()

    result = adapter.discover(provider, None, ProviderDiscoveryPolicy())
    refresh_result = refresh_model_discovery(cfg, "anthropic", http_client=client)

    assert result.ok is True
    assert result.source == "static_catalog"
    assert result.provider_id == "anthropic"
    assert result.network_accessed is False
    assert result.credentials_included is False
    assert result.model_count == 1
    model = result.models[0]
    assert model.raw_model_ref == "anthropic/claude-haiku-4-20250514"
    assert model.protocol == "anthropic_messages"
    assert model.context_limit == 200000
    assert model.max_output_tokens == 8192
    assert model.modalities == ["text", "image"]
    assert model.reasoning_support == "tokens"
    assert model.tool_support is True
    assert model.release_date == "2025-05-14"
    assert model.family == "claude-4"
    assert model.network_accessed is False
    assert model.credentials_included is False
    assert model.discovery_metadata["static_catalog"] is True
    assert model.discovery_metadata["release_date"] == "2025-05-14"
    assert model.discovery_metadata["family"] == "claude-4"
    assert refresh_result.source == "static_catalog"
    assert refresh_result.network_accessed is False
    assert [item.raw_model_ref for item in refresh_result.models] == ["anthropic/claude-haiku-4-20250514"]
    assert client.gets == []


def test_provider_catalog_redacts_credentials_and_marks_disabled_backend() -> None:
    cfg = default_config()
    providers = list_provider_catalog(cfg)
    by_id = {provider.provider_id: provider for provider in providers}

    assert by_id["codex_cli"].credential_status == ProviderCredentialStatus.CONFIGURED
    assert by_id["paid_openai_compatible"].enabled is False
    assert by_id["paid_openai_compatible"].credential_status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["paid_openai_compatible"].constraints
    assert by_id["codex_cli"].policy_boundary == {
        "kind": "provider_catalog_metadata",
        "source": "provider_model_catalog",
        "metadata_only": True,
    }
    assert by_id["codex_cli"].metadata_only is True
    assert by_id["codex_cli"].provider_execution_started is False
    assert by_id["codex_cli"].model_execution_started is False
    assert by_id["codex_cli"].network_accessed is False
    assert by_id["codex_cli"].credentials_included is False
    assert by_id["codex_cli"].credential_write_supported is False
    assert by_id["codex_cli"].credential_written is False
    assert by_id["codex_cli"].refresh_supported is False
    assert by_id["codex_cli"].hidden_provider_fallback is False
    assert by_id["codex_cli"].hidden_model_fallback is False
    assert by_id["codex_cli"].no_hidden_fallback is True
    assert by_id["codex_cli"].permission_granting is False
    assert by_id["codex_cli"].authority_granting is False

    serialized = json.dumps([provider.model_dump(mode="json") for provider in providers])
    assert "ollama" not in serialized
    assert "api_key" not in serialized
    assert "auth_mode" not in serialized


def test_catalog_metadata_resolution_never_includes_secret_material(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-catalog-secret")
    cfg = default_config()
    provider = next(provider for provider in list_provider_catalog(cfg) if provider.provider_id == "paid_openai_compatible")
    descriptor = next(item for item in build_provider_descriptors(cfg) if item.provider_id == "paid_openai_compatible")

    credential = resolve_provider_credential(cfg, descriptor, allow_secret_material=False)
    payload = credential.model_dump(mode="json")
    serialized = json.dumps({"provider": provider.model_dump(mode="json"), "credential": payload})

    assert credential.status == "configured"
    assert credential.env_var == "OPENAI_API_KEY"
    assert credential.api_key is None
    assert credential.headers == {}
    assert credential.credential_value_included is False
    assert credential.credentials_included is False
    assert "sk-catalog-secret" not in serialized


def test_provider_account_store_updates_provider_credential_status_without_secret_leakage(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    cfg = default_config()
    store = SQLiteStore(tmp_path)

    account = store.create_provider_account(
        provider_id="paid_openai_compatible",
        credential_kind="api_key",
        status="configured",
        description="team key",
        metadata={"label": "team", "secret_preview": "sk-redacted"},
    )
    accounts = store.list_provider_accounts("paid_openai_compatible")
    providers = list_provider_catalog(cfg, provider_accounts=store.list_provider_accounts())
    by_id = {provider.provider_id: provider for provider in providers}
    status = runner.invoke(app, ["providers", "status", "--project", str(tmp_path), "--output", "json"])

    assert account["schema_version"] == "harness.provider_account/v1"
    assert account["provider_id"] == "paid_openai_compatible"
    assert account["credential_kind"] == "api_key"
    assert account["status"] == "configured"
    assert account["active"] is True
    assert account["credential_value_included"] is False
    assert account["credentials_included"] is False
    assert account["credential_written"] is False
    assert len(accounts) == 1
    assert by_id["paid_openai_compatible"].credential_status == ProviderCredentialStatus.CONFIGURED
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    provider_payload = {item["provider_id"]: item for item in payload["providers"]}
    assert provider_payload["paid_openai_compatible"]["credential_status"] == "configured"
    serialized = json.dumps({"account": account, "providers": payload["providers"]})
    assert "OPENAI_API_KEY" in serialized
    assert "sk-redacted" not in serialized
    assert "credential_value_included" in serialized
    assert payload["credentials_included"] is False
    assert payload["credential_written"] is False
    assert "OPENAI_API_KEY" in serialized
    assert "silently falling back" in serialized.lower()


def test_active_provider_state_merges_config_env_accounts_and_discovery(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-env")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)
    account = store.create_provider_account(
        provider_id="paid_openai_compatible",
        credential_kind="env",
        status="configured",
        description="registry env",
        metadata={"env_var": "OPENAI_API_KEY"},
    )
    refresh_model_discovery(
        cfg,
        "paid_openai_compatible",
        store=store,
        http_client=FakeDiscoveryHttpClient(),
        approve_hosted=True,
    )
    discovered = list_cached_discovered_models(cfg, store)

    registry = ActiveProviderRegistry(cfg, store=store, model_overlays=discovered)
    provider = registry.get_provider("paid_openai_compatible")
    discovered_model = registry.get_model("paid_openai_compatible", "alpha-local")

    assert provider is not None
    assert provider.provider_id == "paid_openai_compatible"
    assert provider.enabled is False
    assert provider.connected is False
    assert provider.credential_status == "configured"
    assert provider.credential_source == "provider_account"
    assert provider.credential_kind == "env"
    assert provider.account_id == account["account_id"]
    assert provider.catalog_source == "backend_config"
    assert provider.model_count >= 2
    assert provider.default_model_candidate == "paid_openai_compatible/gpt-5.3-codex"
    assert provider.blocked_reasons == ["provider_disabled"]
    assert discovered_model is not None
    assert discovered_model.known_catalog_model is True
    assert discovered_model.source == "discovered"
    assert discovered_model.available_model is False
    assert discovered_model.blocked_reasons == ["provider_disabled"]
    assert "sk-registry-env" not in json.dumps(provider.model_dump(mode="json"))


def test_available_models_exclude_missing_credentials_but_catalog_keeps_them_visible(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["backends"]["paid_openai_compatible"]["settings"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    registry = ActiveProviderRegistry(cfg, store=store)
    available_refs = {model.raw_model_ref for model in registry.list_available_models()}
    catalog = {model.raw_model_ref: model for model in list_model_catalog(cfg, provider_accounts=store.list_provider_accounts())}
    validation = validate_model_selection(
        cfg,
        "paid_openai_compatible/gpt-5.3-codex",
        provider_accounts=store.list_provider_accounts(),
    )

    assert "paid_openai_compatible/gpt-5.3-codex" in catalog
    assert catalog["paid_openai_compatible/gpt-5.3-codex"].known_catalog_model is True
    assert catalog["paid_openai_compatible/gpt-5.3-codex"].available_model is False
    assert catalog["paid_openai_compatible/gpt-5.3-codex"].executable_model is False
    assert catalog["paid_openai_compatible/gpt-5.3-codex"].blocked_reasons == ["credential_missing"]
    assert "paid_openai_compatible/gpt-5.3-codex" not in available_refs
    assert validation.known_catalog_entry is True
    assert validation.executable is False
    assert validation.blocked_reasons == ["credential_missing"]


def test_static_catalog_models_are_visible_without_discovery_or_credentials() -> None:
    cfg = default_config()

    catalog = {model.raw_model_ref: model for model in list_model_catalog(cfg)}
    validation = validate_model_selection(cfg, "google/gemini-2.5-pro")

    assert "google/gemini-2.5-pro" in catalog
    model = catalog["google/gemini-2.5-pro"]
    assert model.source == "static_catalog"
    assert model.known_catalog_model is True
    assert model.available_model is False
    assert model.executable_model is False
    assert model.blocked_reasons == ["provider_disabled"]
    assert model.network_accessed is False
    assert model.credentials_included is False
    assert model.context_limit == 1048576
    assert model.max_output_tokens == 8192
    assert model.modalities == ["text", "image"]
    assert model.reasoning_support == "tokens"
    assert model.tool_support is True
    assert model.release_date == "2025-06-17"
    assert model.family == "gemini-2.5"
    assert validation.known_catalog_entry is True
    assert validation.provider_known is True
    assert validation.executable is False
    assert validation.blocked_reasons == ["provider_disabled"]


def test_suggestions_do_not_change_validation_result(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)
    registry = ActiveProviderRegistry(cfg, store=store)

    before = validate_model_selection(cfg, "gpt-5.5", provider_accounts=store.list_provider_accounts())
    suggestions = registry.suggest_models("gpt 5.5")
    after = validate_model_selection(cfg, "gpt-5.5", provider_accounts=store.list_provider_accounts())

    assert suggestions
    assert suggestions[0]["suggestion_only"] is True
    assert suggestions[0]["selected_model"] is False
    assert suggestions[0]["provider_execution_started"] is False
    assert before.model_dump(mode="json") == after.model_dump(mode="json")
    assert after.executable is False
    assert "provider_not_specified" in after.blocked_reasons


def test_model_catalog_lists_backend_and_profile_model_refs_without_fallback() -> None:
    cfg = default_config()
    models = list_model_catalog(cfg)
    refs = {(model.provider_id, model.model_profile_id, model.raw_model_ref) for model in models}

    assert ("codex_cli", None, "codex_cli/gpt-5.5") in refs
    assert ("local_openai_compatible", None, "local_openai_compatible/qwen3-coder:30b") in refs
    assert ("anthropic", None, "anthropic/claude-sonnet-4-20250514") in refs
    assert ("bedrock", None, "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0") in refs
    assert ("google", None, "google/gemini-2.5-flash") in refs
    assert ("codex_cli", "codex_supervised", "codex_cli/gpt-5.5") in refs
    assert ("local_openai_compatible", "local_reasoning", "local_openai_compatible/qwen3-coder:30b") in refs
    assert ("codex", None, "codex/gpt-5.5") in refs
    assert ("local", None, "local/qwen3-coder") in refs
    assert ("openai", None, "openai/gpt-5.3-codex") in refs
    aliases = {model.raw_model_ref: model for model in models if model.source == "alias"}
    assert aliases["codex/gpt-5.5"].canonical_model_ref == "codex_cli/gpt-5.5"
    assert aliases["codex/gpt-5.5"].alias_of == "codex_cli/gpt-5.5"
    assert aliases["local/qwen3-coder"].canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert all("fallback" in " ".join(model.safety_notes) for model in models)
    assert all(model.policy_boundary["kind"] == "model_catalog_metadata" for model in models)
    assert all(model.metadata_only is True for model in models)
    assert all(model.provider_execution_started is False for model in models)
    assert all(model.model_execution_started is False for model in models)
    assert all(model.network_accessed is False for model in models)
    assert all(model.credentials_included is False for model in models)
    assert all(model.refresh_supported is False for model in models)
    assert all(model.hidden_provider_fallback is False for model in models)
    assert all(model.hidden_model_fallback is False for model in models)
    assert all(model.no_hidden_fallback is True for model in models)
    assert all(model.permission_granting is False for model in models)
    assert all(model.authority_granting is False for model in models)


def test_model_selection_validation_is_deterministic_and_never_falls_back() -> None:
    cfg = default_config()

    known = validate_model_selection(cfg, "codex_cli/gpt-5.5")
    known_local = validate_model_selection(cfg, "local_openai_compatible/qwen3-coder:30b")
    disabled = validate_model_selection(cfg, "paid_openai_compatible/gpt-5.3-codex")
    unknown_provider = validate_model_selection(cfg, "missing/gpt-5.5")
    unknown_model = validate_model_selection(cfg, "codex_cli/not-a-real-model")
    unspecified_provider = validate_model_selection(cfg, "gpt-5.5")
    missing = validate_model_selection(cfg, None)
    alias = validate_model_selection(cfg, "codex/gpt-5.5")
    local_alias = validate_model_selection(cfg, "local/qwen3-coder")
    disabled_alias = validate_model_selection(cfg, "openai/gpt-5.3-codex")
    anthropic_disabled = validate_model_selection(cfg, "anthropic/claude-sonnet-4-20250514")
    bedrock_disabled = validate_model_selection(cfg, "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
    google_disabled = validate_model_selection(cfg, "google/gemini-2.5-flash")

    assert known.schema_version == "harness.model_selection_validation/v1"
    assert known.known_catalog_entry is True
    assert known.provider_known is True
    assert known.provider_enabled is True
    assert known.executable is True
    assert known.canonical_model_ref == "codex_cli/gpt-5.5"
    assert known.protocol == "codex_cli"
    assert known.alias_used is None
    assert known.resolved_model_selection is not None
    assert known.resolved_model_selection.model.protocol == "codex_cli"
    assert known.resolved_model_selection.provider.provider_id == "codex_cli"
    assert known.blocked_reasons == []
    assert known.matched_model is not None
    assert known.matched_model.raw_model_ref == "codex_cli/gpt-5.5"
    assert known_local.executable is True
    assert known_local.model_id == "qwen3-coder:30b"
    assert known_local.variant is None
    assert known_local.canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert known_local.protocol == "openai_chat"
    assert known_local.resolved_model_selection is not None
    assert known_local.resolved_model_selection.resolved_endpoint == "http://localhost:11434/v1"
    assert alias.raw_model_ref == "codex/gpt-5.5"
    assert alias.known_catalog_entry is True
    assert alias.provider_id == "codex_cli"
    assert alias.model_id == "gpt-5.5"
    assert alias.canonical_model_ref == "codex_cli/gpt-5.5"
    assert alias.alias_used == "codex/gpt-5.5"
    assert alias.protocol == "codex_cli"
    assert alias.executable is True
    assert alias.blocked_reasons == []
    assert alias.matched_model is not None
    assert alias.matched_model.raw_model_ref == "codex/gpt-5.5"
    assert alias.matched_model.alias_of == "codex_cli/gpt-5.5"
    assert alias.resolved_model_selection is not None
    assert alias.resolved_model_selection.provider.provider_id == "codex_cli"
    assert alias.resolved_model_selection.model.raw_model_ref == "codex_cli/gpt-5.5"
    assert local_alias.provider_id == "local_openai_compatible"
    assert local_alias.model_id == "qwen3-coder:30b"
    assert local_alias.canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert local_alias.alias_used == "local/qwen3-coder"
    assert local_alias.protocol == "openai_chat"
    assert local_alias.executable is True
    assert known.policy_boundary == {
        "kind": "model_selection_validation",
        "source": "provider_model_catalog",
        "metadata_only": True,
    }
    for result in (known, disabled, unknown_provider, unknown_model, unspecified_provider, missing, alias, local_alias, disabled_alias, anthropic_disabled, bedrock_disabled, google_disabled):
        assert result.metadata_only is True
        assert result.provider_execution_started is False
        assert result.model_execution_started is False
        assert result.network_accessed is False
        assert result.credentials_included is False
        assert result.refresh_supported is False
        assert result.hidden_provider_fallback is False
        assert result.hidden_model_fallback is False
        assert result.no_hidden_fallback is True
        assert result.permission_granting is False
        assert result.authority_granting is False

    assert disabled.known_catalog_entry is True
    assert disabled.provider_known is True
    assert disabled.provider_enabled is False
    assert disabled.executable is False
    assert disabled.canonical_model_ref == "paid_openai_compatible/gpt-5.3-codex"
    assert disabled.protocol == "openai_codex_responses"
    assert disabled.resolved_model_selection is not None
    assert disabled.blocked_reasons == ["provider_disabled"]
    assert disabled_alias.known_catalog_entry is True
    assert disabled_alias.provider_known is True
    assert disabled_alias.provider_enabled is False
    assert disabled_alias.executable is False
    assert disabled_alias.provider_id == "paid_openai_compatible"
    assert disabled_alias.model_id == "gpt-5.3-codex"
    assert disabled_alias.canonical_model_ref == "paid_openai_compatible/gpt-5.3-codex"
    assert disabled_alias.alias_used == "openai/gpt-5.3-codex"
    assert disabled_alias.blocked_reasons == ["provider_disabled"]
    assert anthropic_disabled.known_catalog_entry is True
    assert anthropic_disabled.provider_known is True
    assert anthropic_disabled.provider_enabled is False
    assert anthropic_disabled.executable is False
    assert anthropic_disabled.canonical_model_ref == "anthropic/claude-sonnet-4-20250514"
    assert anthropic_disabled.protocol == "anthropic_messages"
    assert anthropic_disabled.blocked_reasons == ["provider_disabled"]
    assert bedrock_disabled.known_catalog_entry is True
    assert bedrock_disabled.provider_known is True
    assert bedrock_disabled.provider_enabled is False
    assert bedrock_disabled.executable is False
    assert bedrock_disabled.canonical_model_ref == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert bedrock_disabled.protocol == "bedrock_converse"
    assert bedrock_disabled.blocked_reasons == ["provider_disabled"]
    assert google_disabled.known_catalog_entry is True
    assert google_disabled.provider_known is True
    assert google_disabled.provider_enabled is False
    assert google_disabled.executable is False
    assert google_disabled.canonical_model_ref == "google/gemini-2.5-flash"
    assert google_disabled.protocol == "google_generative"
    assert google_disabled.blocked_reasons == ["provider_disabled"]
    assert unknown_provider.provider_known is False
    assert unknown_provider.executable is False
    assert unknown_provider.blocked_reasons == ["provider_unknown", "model_unknown"]
    assert unknown_model.provider_known is True
    assert unknown_model.known_catalog_entry is False
    assert unknown_model.executable is False
    assert unknown_model.blocked_reasons == ["model_unknown"]
    assert unspecified_provider.provider_id is None
    assert unspecified_provider.executable is False
    assert unspecified_provider.blocked_reasons == ["provider_not_specified", "model_unknown"]
    assert missing.executable is False
    assert missing.blocked_reasons == ["model_ref_missing", "provider_not_specified"]


def test_model_selection_validation_reports_unknown_variant_without_execution() -> None:
    result = validate_model_selection(default_config(), "codex_cli/gpt-5.5@ultra")

    assert result.raw_model_ref == "codex_cli/gpt-5.5@ultra"
    assert result.provider_id == "codex_cli"
    assert result.model_id == "gpt-5.5"
    assert result.variant == "ultra"
    assert result.known_catalog_entry is True
    assert result.provider_known is True
    assert result.provider_enabled is True
    assert result.executable is False
    assert result.canonical_model_ref is None
    assert result.protocol is None
    assert result.resolved_model_selection is None
    assert result.blocked_reasons == ["variant_unknown"]
    assert result.provider_execution_started is False
    assert result.model_execution_started is False
    assert result.network_accessed is False
    assert result.credentials_included is False
    assert result.hidden_provider_fallback is False
    assert result.hidden_model_fallback is False
    assert result.no_hidden_fallback is True


def test_model_selection_validation_reports_reasoning_option_blocks_without_execution() -> None:
    result = validate_model_selection(
        default_config(),
        "local_openai_compatible/qwen3-coder:30b",
        request_options={"model_reasoning_effort": "high"},
    )

    assert result.raw_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert result.provider_id == "local_openai_compatible"
    assert result.known_catalog_entry is True
    assert result.provider_known is True
    assert result.provider_enabled is True
    assert result.executable is False
    assert result.resolved_model_selection is None
    assert result.blocked_reasons == ["reasoning_effort_unsupported"]
    assert result.provider_execution_started is False
    assert result.model_execution_started is False
    assert result.network_accessed is False


def test_provider_and_model_catalog_cli_json_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    providers = runner.invoke(app, ["providers", "list", "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["providers", "status", "--project", str(tmp_path), "--output", "json"])
    models = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "codex_cli", "--output", "json"])
    model_providers = runner.invoke(app, ["models", "providers", "--project", str(tmp_path), "--output", "json"])
    model_inspect = runner.invoke(app, ["models", "inspect", "codex/gpt-5.5", "--project", str(tmp_path), "--output", "json"])
    model_protocols = runner.invoke(app, ["models", "protocols", "--project", str(tmp_path), "--output", "json"])
    model_preferences = runner.invoke(app, ["models", "preferences", "--project", str(tmp_path), "--output", "json"])
    blocked_validate = runner.invoke(
        app,
        ["models", "validate", "openai/gpt-5.3-codex", "--project", str(tmp_path), "--output", "json"],
    )

    assert providers.exit_code == 0, providers.output
    assert status.exit_code == 0, status.output
    assert models.exit_code == 0, models.output
    assert model_providers.exit_code == 0, model_providers.output
    assert model_inspect.exit_code == 0, model_inspect.output
    assert model_protocols.exit_code == 0, model_protocols.output
    assert model_preferences.exit_code == 0, model_preferences.output
    assert blocked_validate.exit_code == 1

    providers_payload = json.loads(providers.output)
    status_payload = json.loads(status.output)
    models_payload = json.loads(models.output)
    model_providers_payload = json.loads(model_providers.output)
    model_inspect_payload = json.loads(model_inspect.output)
    model_protocols_payload = json.loads(model_protocols.output)
    model_preferences_payload = json.loads(model_preferences.output)
    blocked_validate_payload = json.loads(blocked_validate.output)
    assert providers_payload["schema_version"] == "harness.providers/v1"
    assert providers_payload["policy_boundary"]["kind"] == "providers_catalog_projection"
    assert providers_payload["metadata_only"] is True
    assert providers_payload["provider_execution_started"] is False
    assert providers_payload["model_execution_started"] is False
    assert providers_payload["network_accessed"] is False
    assert providers_payload["credentials_included"] is False
    assert providers_payload["credential_write_supported"] is False
    assert providers_payload["credential_written"] is False
    assert providers_payload["refresh_supported"] is False
    assert providers_payload["hidden_provider_fallback"] is False
    assert providers_payload["hidden_model_fallback"] is False
    assert providers_payload["permission_granting"] is False
    assert providers_payload["authority_granting"] is False
    assert providers_payload["no_hidden_fallback"] is True
    assert providers_payload["cache"]["provider_count"] == len(providers_payload["providers"])
    assert providers_payload["cache"]["permission_granting"] is False
    assert status_payload["schema_version"] == "harness.providers_status/v1"
    assert status_payload["policy_boundary"]["kind"] == "providers_status_projection"
    assert status_payload["metadata_only"] is True
    assert status_payload["provider_execution_started"] is False
    assert status_payload["credential_written"] is False
    assert models_payload["schema_version"] == "harness.models/v1"
    assert models_payload["policy_boundary"]["kind"] == "models_catalog_projection"
    assert models_payload["metadata_only"] is True
    assert models_payload["provider_execution_started"] is False
    assert models_payload["model_execution_started"] is False
    assert models_payload["network_accessed"] is False
    assert models_payload["credentials_included"] is False
    assert models_payload["refresh_supported"] is False
    assert models_payload["hidden_provider_fallback"] is False
    assert models_payload["hidden_model_fallback"] is False
    assert models_payload["permission_granting"] is False
    assert models_payload["authority_granting"] is False
    assert models_payload["no_hidden_fallback"] is True
    assert {model["provider_id"] for model in models_payload["models"]} == {"codex_cli"}
    assert all(model["protocol"] == "codex_cli" for model in models_payload["models"])
    assert all(model["status"] == "active" for model in models_payload["models"])
    assert model_providers_payload["schema_version"] == "harness.models_providers/v1"
    assert model_providers_payload["network_accessed"] is False
    assert {provider["provider_id"] for provider in model_providers_payload["providers"]} == {
        "anthropic",
        "bedrock",
        "codex_cli",
        "google",
        "local_openai_compatible",
        "paid_openai_compatible",
    }
    assert model_inspect_payload["schema_version"] == "harness.model_inspection/v1"
    assert model_inspect_payload["validation"]["raw_model_ref"] == "codex/gpt-5.5"
    assert model_inspect_payload["validation"]["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert model_inspect_payload["validation"]["alias_used"] == "codex/gpt-5.5"
    assert model_inspect_payload["validation"]["executable"] is True
    assert model_inspect_payload["network_accessed"] is False
    assert model_protocols_payload["schema_version"] == "harness.model_protocols/v1"
    assert [item["protocol"] for item in model_protocols_payload["protocols"]] == [
        "anthropic_messages",
        "bedrock_converse",
        "codex_cli",
        "google_generative",
        "openai_chat",
        "openai_codex_responses",
        "openai_responses",
    ]
    assert model_protocols_payload["provider_execution_started"] is False
    assert model_preferences_payload["schema_version"] == "harness.model_preferences/v1"
    assert model_preferences_payload["preferences"] == []
    assert model_preferences_payload["provider_execution_started"] is False
    assert model_preferences_payload["credential_written"] is False
    assert blocked_validate_payload["validation"]["blocked_reasons"] == ["provider_disabled"]
    assert blocked_validate_payload["validation"]["canonical_model_ref"] == "paid_openai_compatible/gpt-5.3-codex"
    assert "api_key" not in providers.output

    verbose = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "codex_cli", "--verbose"])
    assert verbose.exit_code == 0, verbose.output
    assert "reasoning" in verbose.output
    assert "modalities" in verbose.output
    assert "cost" in verbose.output
    assert "text" in verbose.output
    assert "codex_cli/gpt-5.5" in verbose.output

    cached = SQLiteStore(tmp_path).list_provider_model_catalog_cache()
    assert {row["catalog_kind"] for row in cached} == {"provider", "model"}
    assert any(row["provider_id"] == "codex_cli" and row["catalog_kind"] == "provider" for row in cached)
    assert any(row["raw_model_ref"] == "codex_cli/gpt-5.5" for row in cached)
    serialized_cache = json.dumps(cached)
    assert "api_key" not in serialized_cache
    assert "ollama" not in serialized_cache


def test_custom_openai_compatible_provider_from_config_is_cataloged_without_secret_leakage(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    template = config_data["backends"]["paid_openai_compatible"]
    config_data["backends"]["team_openai_compatible"] = {
        **template,
        "name": "team_openai_compatible",
        "settings": {
            **template["settings"],
            "enabled": True,
            "base_url": "https://models.example.test/v1",
            "api_key_env": "TEAM_OPENAI_API_KEY",
            "model": "team-coder-1",
            "cost_note": "should not leak",
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    providers = runner.invoke(app, ["providers", "list", "--project", str(tmp_path), "--output", "json"])
    models = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "team_openai_compatible", "--output", "json"])
    validation = runner.invoke(
        app,
        ["models", "validate", "team_openai_compatible/team-coder-1", "--project", str(tmp_path), "--output", "json"],
    )

    assert providers.exit_code == 0, providers.output
    assert models.exit_code == 0, models.output
    assert validation.exit_code == 1, validation.output
    providers_payload = json.loads(providers.output)
    provider = next(item for item in providers_payload["providers"] if item["provider_id"] == "team_openai_compatible")
    assert provider["kind"] == "native_model"
    assert provider["enabled"] is True
    assert provider["credential_status"] == "missing"
    assert provider["settings_preview"]["base_url"] == "https://models.example.test/v1"
    assert provider["settings_preview"]["credential_env"] == "TEAM_OPENAI_API_KEY"
    assert provider["provider_execution_started"] is False
    assert provider["network_accessed"] is False
    assert provider["credentials_included"] is False
    assert provider["no_hidden_fallback"] is True

    model_payload = json.loads(models.output)
    assert model_payload["models"][0]["raw_model_ref"] == "team_openai_compatible/team-coder-1"
    assert model_payload["models"][0]["available_model"] is False
    assert model_payload["models"][0]["executable_model"] is False
    assert model_payload["models"][0]["blocked_reasons"] == ["credential_missing"]
    assert model_payload["models"][0]["provider_execution_started"] is False
    assert model_payload["models"][0]["network_accessed"] is False
    assert model_payload["models"][0]["credentials_included"] is False
    validation_payload = json.loads(validation.output)
    assert validation_payload["validation"]["executable"] is False
    assert validation_payload["validation"]["blocked_reasons"] == ["credential_missing"]
    serialized = providers.output + models.output + validation.output
    assert "api_key" not in serialized
    assert "cost_note" not in serialized
    assert "should not leak" not in serialized


def test_models_validate_cli_reports_fail_closed_model_selection(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    known = runner.invoke(
        app,
        ["models", "validate", "codex_cli/gpt-5.5", "--project", str(tmp_path), "--output", "json"],
    )
    unknown = runner.invoke(
        app,
        ["models", "validate", "codex_cli/not-a-real-model", "--project", str(tmp_path), "--output", "json"],
    )

    assert known.exit_code == 0, known.output
    assert unknown.exit_code == 1
    known_payload = json.loads(known.output)
    unknown_payload = json.loads(unknown.output)
    assert known_payload["schema_version"] == "harness.model_selection_validation_result/v1"
    assert known_payload["ok"] is True
    assert known_payload["validation"]["executable"] is True
    assert known_payload["validation"]["known_catalog_entry"] is True
    assert known_payload["validation"]["provider_execution_started"] is False
    assert known_payload["validation"]["model_execution_started"] is False
    assert known_payload["validation"]["hidden_provider_fallback"] is False
    assert known_payload["validation"]["hidden_model_fallback"] is False
    assert known_payload["validation"]["no_hidden_fallback"] is True
    assert unknown_payload["ok"] is False
    assert unknown_payload["validation"]["executable"] is False
    assert unknown_payload["validation"]["blocked_reasons"] == ["model_unknown"]
    assert unknown_payload["validation"]["provider_known"] is True
    assert unknown_payload["validation"]["provider_enabled"] is True
    assert unknown_payload["validation"]["provider_execution_started"] is False
    assert unknown_payload["validation"]["network_accessed"] is False
    assert unknown_payload["validation"]["permission_granting"] is False
    assert unknown_payload["validation"]["authority_granting"] is False


def test_session_model_cli_persists_selection_and_validation_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model switch")

    known = runner.invoke(
        app,
        ["session", "model", session.id, "codex_cli/gpt-5.5", "--project", str(tmp_path), "--output", "json"],
    )
    unknown = runner.invoke(
        app,
        ["session", "model", session.id, "codex_cli/not-a-real-model", "--project", str(tmp_path), "--output", "json"],
    )

    assert known.exit_code == 0, known.output
    assert unknown.exit_code == 1
    known_payload = json.loads(known.output)
    unknown_payload = json.loads(unknown.output)
    assert known_payload["schema_version"] == "harness.session_model_selection/v1"
    assert known_payload["ok"] is True
    assert known_payload["session"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert known_payload["session"]["provider_id"] == "codex_cli"
    assert known_payload["model_validation"]["executable"] is True
    assert known_payload["provider_execution_started"] is False
    assert known_payload["model_execution_started"] is False
    assert known_payload["hidden_model_fallback"] is False
    assert known_payload["no_hidden_fallback"] is True
    assert known_payload["permission_granting"] is False
    assert known_payload["authority_granting"] is False
    assert unknown_payload["ok"] is False
    assert unknown_payload["session"]["raw_model_ref"] == "codex_cli/not-a-real-model"
    assert unknown_payload["model_validation"]["executable"] is False
    assert unknown_payload["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert unknown_payload["hidden_provider_fallback"] is False
    assert unknown_payload["hidden_model_fallback"] is False
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    model_events = [event for event in events if event.kind == "session.model_selected"]
    validation_events = [event for event in events if event.kind == "session.model_validation"]
    assert [event.payload["raw_model_ref"] for event in model_events] == [
        "codex_cli/gpt-5.5",
        "codex_cli/not-a-real-model",
    ]
    assert [event.payload["source"] for event in validation_events] == [
        "session_model_command",
        "session_model_command",
    ]
    assert all(event.payload["provider_execution_started"] is False for event in validation_events)
    assert all(event.payload["hidden_model_fallback"] is False for event in validation_events)
    preferences = SQLiteStore(tmp_path).list_model_preferences()
    assert [preference["raw_model_ref"] for preference in preferences] == ["codex_cli/gpt-5.5"]
    assert preferences[0]["selection_count"] == 1
    assert preferences[0]["favorite"] is False
    assert preferences[0]["network_accessed"] is False
    assert preferences[0]["credentials_included"] is False


def test_model_preference_cli_favorites_default_and_projection_order(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model preferences")

    selected = runner.invoke(
        app,
        ["session", "model", session.id, "local_openai_compatible/qwen3-coder:30b", "--project", str(tmp_path), "--output", "json"],
    )
    favorite = runner.invoke(
        app,
        ["models", "favorite", "codex_cli/gpt-5.5", "--project", str(tmp_path), "--output", "json"],
    )
    default = runner.invoke(
        app,
        ["models", "default", "codex_cli/gpt-5.5", "--project", str(tmp_path), "--output", "json"],
    )
    preferences = runner.invoke(app, ["models", "preferences", "--project", str(tmp_path), "--output", "json"])

    assert selected.exit_code == 0, selected.output
    assert favorite.exit_code == 0, favorite.output
    assert default.exit_code == 0, default.output
    assert preferences.exit_code == 0, preferences.output
    favorite_payload = json.loads(favorite.output)
    default_payload = json.loads(default.output)
    preferences_payload = json.loads(preferences.output)
    assert favorite_payload["schema_version"] == "harness.model_preference_update/v1"
    assert favorite_payload["preference"]["favorite"] is True
    assert favorite_payload["provider_execution_started"] is False
    assert default_payload["preference"]["is_default"] is True
    assert preferences_payload["default"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    by_ref = {item["raw_model_ref"]: item for item in preferences_payload["preferences"]}
    assert by_ref["codex_cli/gpt-5.5"]["favorite"] is True
    assert by_ref["local_openai_compatible/qwen3-coder:30b"]["selection_count"] == 1
    assert "api_key" not in preferences.output

    dashboard = build_tui_dashboard(tmp_path, selected_session_id=session.id)
    models = dashboard["model_catalog"]["models"]
    model_by_ref = {model["raw_model_ref"]: model for model in models}
    assert model_by_ref["codex_cli/gpt-5.5"]["favorite"] is True
    assert "high" in model_by_ref["codex_cli/gpt-5.5"]["variant_list"]
    assert model_by_ref["local_openai_compatible/qwen3-coder:30b"]["selection_count"] == 1
    assert models[0]["raw_model_ref"] == "local_openai_compatible/qwen3-coder:30b"
    assert "preferences" in dashboard["model_catalog"]


def test_models_refresh_fails_closed_without_provider_network_call(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--refresh", "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "refusing to call providers implicitly" in payload["error"]


def test_explicit_local_model_refresh_discovers_models_and_updates_cache(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    client = FakeDiscoveryHttpClient()

    result = refresh_model_discovery(default_config(), "local_openai_compatible", store=store, http_client=client)

    assert result.ok is True
    assert result.provider_id == "local_openai_compatible"
    assert result.source == "discovered"
    assert result.network_accessed is True
    assert result.credentials_included is False
    assert result.credential_written is False
    assert result.model_count == 2
    assert result.cache is not None
    assert result.cache["cache_ttl_seconds"] == 86400
    assert result.cache["cache_status"] == "fresh"
    assert result.cache["discovery_metadata"]["cache_ttl_seconds"] == 86400
    assert datetime.fromisoformat(result.cache["cache_expires_at"]) > datetime.fromisoformat(result.cache["cache_refreshed_at"])
    assert [model.raw_model_ref for model in result.models] == [
        "local_openai_compatible/alpha-local",
        "local_openai_compatible/zeta-local",
    ]
    assert all(model.source == "discovered" for model in result.models)
    assert all(model.network_accessed is True for model in result.models)
    assert client.gets == [
        {
            "url": "http://localhost:11434/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]
    cached = store.list_provider_model_catalog_cache("model")
    discovered = [row for row in cached if row["payload"]["source"] == "discovered"]
    assert {row["raw_model_ref"] for row in discovered} == {
        "local_openai_compatible/alpha-local",
        "local_openai_compatible/zeta-local",
    }
    assert all(row["payload"]["network_accessed"] is True for row in discovered)
    assert all(row["payload"]["cache_ttl_seconds"] == 86400 for row in discovered)
    assert all(row["payload"]["cache_status"] == "fresh" for row in discovered)
    assert all(row["payload"]["discovery_metadata"]["cache_expires_at"] for row in discovered)
    assert all(row["payload"]["discovery_metadata"]["cache_status"] == "fresh" for row in discovered)

    cli_client = FakeDiscoveryHttpClient()
    monkeypatch.setattr(model_discovery, "UrllibOpenAICompatibleHttpClient", lambda: cli_client)
    cli_result = runner.invoke(
        app,
        ["models", "refresh", "local_openai_compatible", "--project", str(tmp_path), "--output", "json"],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_payload = json.loads(cli_result.output)
    assert cli_payload["ok"] is True
    assert cli_payload["provider_id"] == "local_openai_compatible"
    assert cli_payload["model_count"] == 2
    assert cli_payload["models"][0]["source"] == "discovered"
    assert cli_payload["network_accessed"] is True
    assert cli_payload["cache"]["cache_ttl_seconds"] == 86400
    assert cli_payload["cache"]["cache_status"] == "fresh"
    assert cli_payload["cache"]["discovery_metadata"]["cache_expires_at"]
    assert cli_payload["raw_provider_response_sha256"]
    assert cli_payload["approval_evidence"]["local_endpoint_validated"] is True
    assert cli_client.gets[0]["url"] == "http://localhost:11434/v1/models"

    listed = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "local_openai_compatible", "--output", "json"])
    inspected = runner.invoke(app, ["models", "inspect", "local_openai_compatible/alpha-local", "--project", str(tmp_path), "--output", "json"])
    validated = runner.invoke(app, ["models", "validate", "local_openai_compatible/alpha-local", "--project", str(tmp_path), "--output", "json"])
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    listed_refs = {model["raw_model_ref"] for model in listed_payload["models"]}
    assert "local_openai_compatible/alpha-local" in listed_refs
    assert "local_openai_compatible/zeta-local" in listed_refs
    listed_discovered = [model for model in listed_payload["models"] if model["source"] == "discovered"]
    assert all(model["discovery_metadata"]["raw_provider_response_sha256"] for model in listed_discovered)
    assert all(model["discovery_metadata"]["cache_ttl_seconds"] == 86400 for model in listed_discovered)
    assert all(model["discovery_metadata"]["cache_status"] == "fresh" for model in listed_discovered)
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["validation"]["known_catalog_entry"] is True
    assert inspected_payload["model"]["source"] == "discovered"
    assert validated.exit_code == 0, validated.output

    cleared = runner.invoke(
        app,
        ["models", "refresh", "local_openai_compatible", "--clear-cache", "--project", str(tmp_path), "--output", "json"],
    )
    assert cleared.exit_code == 0, cleared.output
    clear_payload = json.loads(cleared.output)
    assert clear_payload["network_accessed"] is False
    assert clear_payload["cache"]["removed_count"] == 2
    relisted = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "local_openai_compatible", "--output", "json"])
    assert relisted.exit_code == 0, relisted.output
    relisted_refs = {model["raw_model_ref"] for model in json.loads(relisted.output)["models"]}
    assert "local_openai_compatible/alpha-local" not in relisted_refs
    assert "local_openai_compatible/qwen3-coder:30b" in relisted_refs


def test_discovery_cache_merge_preserves_static_metadata_on_refresh_failure(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "router_local": {
                        "display_name": "Router Local",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {
                            "seed-model": {
                                "context_window": 32768,
                                "max_output_tokens": 4096,
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)
    refresh_model_discovery(cfg, "router_local", store=store, http_client=FakeDiscoveryHttpClient())
    before_cache = store.list_provider_model_catalog_cache("model")
    before_discovered_payloads = [
        json.dumps(row["payload"], sort_keys=True)
        for row in before_cache
        if row["provider_id"] == "router_local" and row["payload"]["source"] == "discovered"
    ]
    invalid_client = InvalidDiscoveryHttpClient()

    try:
        refresh_model_discovery(cfg, "router_local", store=store, http_client=invalid_client)
    except model_discovery.ModelDiscoveryError as exc:
        assert exc.blocked_reasons == ["invalid_models_response"]
    else:
        raise AssertionError("invalid discovery response must fail before replacing the cache")

    cached = list_cached_discovered_models(cfg, store, provider_id="router_local")
    catalog = list_model_catalog(cfg, provider_id="router_local", model_overlays=cached)
    by_ref = {model.raw_model_ref: model for model in catalog}
    after_cache = store.list_provider_model_catalog_cache("model")
    after_discovered_payloads = [
        json.dumps(row["payload"], sort_keys=True)
        for row in after_cache
        if row["provider_id"] == "router_local" and row["payload"]["source"] == "discovered"
    ]

    assert invalid_client.gets == [
        {
            "url": "http://localhost:11434/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]
    assert after_discovered_payloads == before_discovered_payloads
    assert [model.raw_model_ref for model in cached] == ["router_local/alpha-local", "router_local/zeta-local"]
    assert by_ref["router_local/seed-model"].source == "custom_config"
    assert by_ref["router_local/seed-model"].context_limit == 32768
    assert by_ref["router_local/alpha-local"].source == "discovered"
    assert by_ref["router_local/alpha-local"].cache_status == "fresh"
    assert by_ref["router_local/alpha-local"].discovery_metadata["cache_status"] == "fresh"


def test_stale_discovery_cache_is_visible_in_catalog_projection(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    refresh_model_discovery(default_config(), "local_openai_compatible", store=store, http_client=FakeDiscoveryHttpClient())
    stale_refresh = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    stale_expiry = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM provider_model_catalog_cache WHERE id LIKE 'catalog_discovered_%' AND provider_id = ?",
            ("local_openai_compatible",),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            metadata = dict(payload.get("discovery_metadata") or {})
            metadata.update(
                {
                    "cache_refreshed_at": stale_refresh,
                    "cache_ttl_seconds": 86400,
                    "cache_expires_at": stale_expiry,
                    "cache_status": "fresh",
                }
            )
            payload.update(
                {
                    "cache_refreshed_at": stale_refresh,
                    "cache_ttl_seconds": 86400,
                    "cache_expires_at": stale_expiry,
                    "cache_status": "fresh",
                    "discovery_metadata": metadata,
                }
            )
            conn.execute(
                "UPDATE provider_model_catalog_cache SET payload_json = ?, refreshed_at = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), stale_refresh, row["id"]),
            )

    listed = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "local_openai_compatible", "--output", "json"])
    inspected = runner.invoke(
        app,
        ["models", "inspect", "local_openai_compatible/alpha-local", "--project", str(tmp_path), "--output", "json"],
    )

    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    stale_models = [model for model in listed_payload["models"] if model["source"] == "discovered"]
    assert stale_models
    assert all(model["cache_status"] == "stale" for model in stale_models)
    assert all(model["last_refresh_at"] == stale_refresh for model in stale_models)
    assert all(model["cache_expires_at"] == stale_expiry for model in stale_models)
    assert all(model["discovery_metadata"]["cache_status"] == "stale" for model in stale_models)
    assert all(model["discovery_metadata"]["cache_refreshed_at"] == stale_refresh for model in stale_models)
    assert inspected.exit_code == 0, inspected.output
    inspected_model = json.loads(inspected.output)["model"]
    assert inspected_model["source"] == "discovered"
    assert inspected_model["cache_status"] == "stale"
    assert inspected_model["last_refresh_at"] == stale_refresh
    assert inspected_model["discovery_metadata"]["cache_status"] == "stale"


def test_explicit_provider_specific_model_refresh_runs_only_through_refresh_command(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    google_client = FakeGoogleDiscoveryHttpClient()
    monkeypatch.setattr(model_discovery, "UrllibOpenAICompatibleHttpClient", lambda: google_client)

    google = runner.invoke(
        app,
        ["models", "refresh", "google", "--approve-hosted", "--project", str(tmp_path), "--output", "json"],
    )
    anthropic = runner.invoke(
        app,
        ["models", "refresh", "anthropic", "--project", str(tmp_path), "--output", "json"],
    )

    assert google.exit_code == 0, google.output
    google_payload = json.loads(google.output)
    assert google_payload["ok"] is True
    assert google_payload["provider_id"] == "google"
    assert google_payload["network_accessed"] is True
    assert google_payload["credentials_included"] is False
    assert google_payload["approval_evidence"]["hosted_refresh_approved"] is True
    assert [model["raw_model_ref"] for model in google_payload["models"]] == ["google/gemini-2.5-flash", "google/gemini-2.5-pro"]
    assert google_payload["cache"]["provider_id"] == "google"
    assert google_payload["cache"]["cache_status"] == "fresh"
    assert google_client.gets == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]

    assert anthropic.exit_code == 0, anthropic.output
    anthropic_payload = json.loads(anthropic.output)
    assert anthropic_payload["ok"] is True
    assert anthropic_payload["provider_id"] == "anthropic"
    assert anthropic_payload["source"] == "static_catalog"
    assert anthropic_payload["network_accessed"] is False
    assert anthropic_payload["credentials_included"] is False
    assert anthropic_payload["approval_evidence"]["static_catalog"] is True
    assert [model["raw_model_ref"] for model in anthropic_payload["models"]] == ["anthropic/claude-haiku-4-20250514"]
    assert anthropic_payload["cache"]["provider_id"] == "anthropic"
    assert anthropic_payload["cache"]["cache_status"] == "fresh"
    assert google_client.gets == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]


def test_models_refresh_metadata_only_and_with_credentials_flags_are_explicit(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setenv("GOOGLE_API_KEY", "google-discovery-secret")
    google_client = FakeGoogleDiscoveryHttpClient()
    monkeypatch.setattr(model_discovery, "UrllibOpenAICompatibleHttpClient", lambda: google_client)

    credentialed = runner.invoke(
        app,
        [
            "models",
            "refresh",
            "google",
            "--approve-hosted",
            "--with-credentials",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    non_metadata = runner.invoke(
        app,
        [
            "models",
            "refresh",
            "google",
            "--approve-hosted",
            "--no-metadata-only",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert credentialed.exit_code == 0, credentialed.output
    credentialed_payload = json.loads(credentialed.output)
    assert credentialed_payload["ok"] is True
    assert credentialed_payload["provider_id"] == "google"
    assert credentialed_payload["credentials_included"] is True
    assert credentialed_payload["credential_written"] is False
    assert credentialed_payload["cache"]["discovery_metadata"]["credentials_included"] is True
    assert "google-discovery-secret" not in credentialed.output
    assert google_client.gets == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer google-discovery-secret",
                "x-goog-api-key": "google-discovery-secret",
            },
            "timeout": 300.0,
        }
    ]

    assert non_metadata.exit_code == 1
    non_metadata_payload = json.loads(non_metadata.output)
    assert non_metadata_payload["ok"] is False
    assert non_metadata_payload["provider_id"] == "google"
    assert non_metadata_payload["blocked_reasons"] == ["metadata_only_required"]
    assert non_metadata_payload["network_accessed"] is False
    assert len(google_client.gets) == 1


def test_explicit_hosted_model_refresh_requires_approval_without_network_call(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["models", "refresh", "openai", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.model_discovery_result/v1"
    assert payload["ok"] is False
    assert payload["provider_id"] == "paid_openai_compatible"
    assert payload["blocked_reasons"] == ["hosted_discovery_approval_required"]
    assert payload["network_accessed"] is False
    assert payload["credentials_included"] is False
    assert payload["credential_written"] is False
    assert payload["no_hidden_fallback"] is True


def test_provider_login_logout_accounts_and_activate_are_explicit_credential_actions(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    before = SQLiteStore(tmp_path).list_provider_model_catalog_cache()

    login = runner.invoke(
        app,
        ["providers", "login", "paid_openai_compatible", "--project", str(tmp_path), "--output", "json"],
        env={"OPENAI_API_KEY": "sk-test-not-printed"},
    )
    accounts = runner.invoke(
        app,
        ["providers", "accounts", "paid_openai_compatible", "--project", str(tmp_path), "--output", "json"],
    )
    status = runner.invoke(app, ["providers", "status", "--project", str(tmp_path), "--output", "json"])
    missing = runner.invoke(app, ["providers", "login", "missing", "--project", str(tmp_path), "--output", "json"])

    assert login.exit_code == 0, login.output
    assert accounts.exit_code == 0, accounts.output
    assert status.exit_code == 0, status.output
    assert missing.exit_code == 1
    login_payload = json.loads(login.output)
    accounts_payload = json.loads(accounts.output)
    status_payload = json.loads(status.output)
    missing_payload = json.loads(missing.output)
    assert login_payload["schema_version"] == "harness.provider_auth/v1"
    assert login_payload["action"] == "login"
    assert login_payload["ok"] is True
    assert login_payload["credential_status"] == "configured"
    assert login_payload["credential_written"] is False
    assert login_payload["credentials_included"] is False
    assert login_payload["permission_granting"] is False
    assert accounts_payload["schema_version"] == "harness.provider_accounts/v1"
    assert accounts_payload["account_count"] == 1
    account_id = accounts_payload["accounts"][0]["account_id"]
    assert accounts_payload["accounts"][0]["credential_kind"] == "env"
    provider_payload = {item["provider_id"]: item for item in status_payload["providers"]}
    assert provider_payload["paid_openai_compatible"]["credential_status"] == "configured"

    second = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "static_local",
            "--description",
            "placeholder",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert second.exit_code == 0, second.output
    second_id = json.loads(second.output)["account"]["account_id"]
    activate = runner.invoke(
        app,
        ["providers", "activate-account", "paid_openai_compatible", account_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert activate.exit_code == 0, activate.output
    activate_payload = json.loads(activate.output)
    assert activate_payload["account"]["account_id"] == account_id
    assert activate_payload["credential_written"] is False
    assert activate_payload["credentials_included"] is False
    assert activate_payload["network_accessed"] is False
    assert activate_payload["no_hidden_fallback"] is True

    logout = runner.invoke(app, ["providers", "logout", "paid_openai_compatible", "--project", str(tmp_path), "--output", "json"])
    assert logout.exit_code == 0, logout.output
    logout_payload = json.loads(logout.output)
    assert logout_payload["action"] == "logout"
    assert logout_payload["removed_account_count"] == 2
    assert {account["account_id"] for account in logout_payload["removed_accounts"]} == {account_id, second_id}
    assert logout_payload["credential_removed"] is True
    assert logout_payload["credentials_included"] is False
    assert logout_payload["no_hidden_fallback"] is True
    assert missing_payload["ok"] is False
    assert "Provider not found: missing" in missing_payload["error"]
    assert before == []
    assert "sk-test-not-printed" not in login.output
    assert "sk-test-not-printed" not in accounts.output
    assert "sk-test-not-printed" not in activate.output
    assert "sk-test-not-printed" not in logout.output


def test_provider_login_api_key_writes_secret_store_and_redacted_account(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    login = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "api_key",
            "--api-key",
            "sk-provider-secret",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert login.exit_code == 0, login.output
    payload = json.loads(login.output)
    account = payload["account"]
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "paid_openai_compatible")
    credential = resolve_provider_credential(cfg, provider, store, allow_secret_material=True)
    secret_path = provider_secret_store_path(tmp_path)
    mode = stat.S_IMODE(secret_path.stat().st_mode)

    assert payload["credential_written"] is True
    assert payload["secret_write"]["credential_written"] is True
    assert account["credential_kind"] == "api_key"
    assert account["credential_value_included"] is False
    assert account["credentials_included"] is False
    assert account["metadata"]["storage"] == "file"
    assert credential.api_key == "sk-provider-secret"
    assert credential.credentials_included is True
    assert mode == 0o600
    assert "sk-provider-secret" not in login.output
    assert "sk-provider-secret" not in json.dumps(store.list_provider_accounts("paid_openai_compatible"))


def test_oauth_refresh_happens_only_for_runtime_resolution(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    provider_oauth_callback(
        tmp_path,
        store,
        cfg,
        "paid_openai_compatible",
        {
            "access_token": "oauth-old-access",
            "refresh_token": "oauth-refresh",
            "expires_at": expired,
        },
    )
    refresh_calls: list[dict] = []

    def fake_refresh(project_root, provider_id, account, refresh_token):
        refresh_calls.append(
            {
                "project_root": project_root,
                "provider_id": provider_id,
                "account_id": account["account_id"],
                "refresh_token": refresh_token,
            }
        )
        return {
            "access_token": "oauth-new-access",
            "refresh_token": "oauth-new-refresh",
            "expires_at": future,
            "network_accessed": True,
        }

    monkeypatch.setattr("harness.provider_auth.refresh_provider_oauth_account", fake_refresh)

    catalog = list_provider_catalog(cfg, provider_accounts=store.list_provider_accounts())
    assert refresh_calls == []
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "paid_openai_compatible")
    credential = resolve_provider_credential(cfg, provider, store, allow_secret_material=True)

    assert refresh_calls == [
        {
            "project_root": tmp_path,
            "provider_id": "paid_openai_compatible",
            "account_id": credential.account_id,
            "refresh_token": "oauth-refresh",
        }
    ]
    assert credential.api_key == "oauth-new-access"
    assert credential.source == "provider_account_oauth_refreshed"
    assert credential.credentials_included is True
    assert "oauth-old-access" not in json.dumps([provider.model_dump(mode="json") for provider in catalog])
    event_kinds = [event.kind for event in store.list_store_events("orchestration", "provider_accounts")]
    assert "provider.oauth_token_refreshed" in event_kinds


def test_expired_oauth_token_blocks_when_refresh_fails_before_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    account = store.create_provider_account(
        provider_id="paid_openai_compatible",
        credential_kind="oauth",
        status="configured",
        expires_at=expired,
        metadata={"oauth_method": "manual_code", "access_secret_ref": "provider_secret_store:access_token"},
    )
    write_provider_oauth_tokens(tmp_path, account, access_token="oauth-expired-access")
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "paid_openai_compatible")

    try:
        resolve_provider_credential(cfg, provider, store, allow_secret_material=True)
    except ProviderCredentialResolutionError as exc:
        error = exc
    else:
        raise AssertionError("Expired OAuth without refresh token should block before provider execution.")

    assert error.reason == "credential_refresh_required"
    event = store.list_store_events("orchestration", "provider_accounts")[-1]
    assert event.kind == "provider.oauth_token_refresh_failed"
    assert event.payload["reason"] == "refresh_token_missing"
    assert event.payload["network_accessed"] is False
    assert event.payload["provider_execution_started"] is False
    assert event.payload["model_execution_started"] is False


def test_provider_logout_removes_secret_payload(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    login = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "api_key",
            "--api-key",
            "sk-delete-me",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    account = json.loads(login.output)["account"]
    assert read_provider_account_secret(tmp_path, account) == "sk-delete-me"

    logout = runner.invoke(app, ["providers", "logout", "paid_openai_compatible", "--project", str(tmp_path), "--output", "json"])

    assert logout.exit_code == 0, logout.output
    payload = json.loads(logout.output)
    assert payload["credential_removed"] is True
    assert payload["removed_accounts"][0]["credential_removed"] is True
    assert read_provider_account_secret(tmp_path, account) is None
    assert "sk-delete-me" not in logout.output
    secret_payload = json.loads(provider_secret_store_path(tmp_path).read_text(encoding="utf-8"))
    assert account["account_id"] not in secret_payload["secrets"]
    event_kinds = [event.kind for event in SQLiteStore(tmp_path).list_store_events("orchestration", "provider_accounts")]
    assert "provider.account_created" in event_kinds
    assert "provider.account_deleted" in event_kinds


def test_provider_account_activation_changes_runtime_resolution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    first = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "api_key",
            "--api-key",
            "sk-first",
            "--description",
            "first",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "api_key",
            "--api-key",
            "sk-second",
            "--description",
            "second",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    first_id = json.loads(first.output)["account"]["account_id"]
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    provider = next(provider for provider in build_provider_descriptors(cfg) if provider.provider_id == "paid_openai_compatible")

    active_second = resolve_provider_credential(cfg, provider, store, allow_secret_material=True)
    activate = runner.invoke(
        app,
        ["providers", "activate-account", "paid_openai_compatible", first_id, "--project", str(tmp_path), "--output", "json"],
    )
    active_first = resolve_provider_credential(cfg, provider, store, allow_secret_material=True)

    assert second.exit_code == 0, second.output
    assert active_second.api_key == "sk-second"
    assert activate.exit_code == 0, activate.output
    assert active_first.api_key == "sk-first"
    assert "sk-first" not in activate.output
    assert "sk-second" not in activate.output
    event_kinds = [event.kind for event in SQLiteStore(tmp_path).list_store_events("orchestration", "provider_accounts")]
    assert "provider.account_activated" in event_kinds


def test_provider_accounts_json_never_contains_secret_value(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    login = runner.invoke(
        app,
        [
            "providers",
            "login",
            "paid_openai_compatible",
            "--credential-kind",
            "api_key",
            "--api-key",
            "sk-never-print",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    accounts = runner.invoke(
        app,
        ["providers", "accounts", "paid_openai_compatible", "--project", str(tmp_path), "--output", "json"],
    )

    assert login.exit_code == 0, login.output
    assert accounts.exit_code == 0, accounts.output
    payload = json.loads(accounts.output)
    assert payload["credentials_included"] is False
    assert payload["accounts"][0]["credential_value_included"] is False
    assert "sk-never-print" not in accounts.output


def test_project_custom_models_config_adds_local_provider_and_models(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "ollama_team": {
                        "display_name": "Ollama Team",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "compatibility": {
                            "supports_developer_role": False,
                            "supports_reasoning_effort": False,
                            "supports_parallel_tool_calls": False,
                            "tool_call_id_policy": "provider_generated",
                            "system_prompt_role": "system",
                            "cache_control": "none",
                        },
                        "models": {
                            "qwen2.5-coder:7b": {
                                "display_name": "Qwen Coder 7B",
                                "api_id": "qwen2.5-coder:7b",
                                "context_window": 32768,
                                "max_output_tokens": 4096,
                                "input_modalities": ["text"],
                                "output_modalities": ["text"],
                                "tool_support": False,
                                "reasoning_support": "none",
                                "status": "active",
                                "variants": {
                                    "fast": {
                                        "display_name": "Fast",
                                        "model_options": {"temperature": 0.1, "max_tokens": 2048},
                                    }
                                },
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    providers = {provider.provider_id: provider for provider in list_provider_catalog(cfg)}
    models = {model.raw_model_ref: model for model in list_model_catalog(cfg)}
    validation = validate_model_selection(cfg, "ollama_team/qwen2.5-coder:7b@fast")
    validate_cli = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path), "--output", "json"])
    list_cli = runner.invoke(
        app,
        ["models", "list", "--provider", "ollama_team", "--project", str(tmp_path), "--output", "json"],
    )

    assert providers["ollama_team"].source == "custom_config"
    assert providers["ollama_team"].settings_preview["base_url"] == "http://localhost:11434/v1"
    assert providers["ollama_team"].credential_status == ProviderCredentialStatus.CONFIGURED
    model = models["ollama_team/qwen2.5-coder:7b"]
    assert model.source == "custom_config"
    assert model.context_limit == 32768
    assert model.reasoning_support == "none"
    assert validation.executable is True
    assert validation.resolved_model_selection is not None
    assert validation.resolved_model_selection.resolved_model_options["max_tokens"] == 2048
    assert validate_cli.exit_code == 0, validate_cli.output
    validate_payload = json.loads(validate_cli.output)
    assert validate_payload["schema_version"] == "harness.custom_models_config_validation/v1"
    assert validate_payload["provider_count"] == 1
    assert validate_payload["model_count"] == 1
    assert validate_payload["validation_issues"] == []
    assert validate_payload["network_accessed"] is False
    assert list_cli.exit_code == 0, list_cli.output
    list_payload = json.loads(list_cli.output)
    assert [item["raw_model_ref"] for item in list_payload["models"]] == ["ollama_team/qwen2.5-coder:7b"]


def test_project_custom_provider_model_filters_and_disabled_models(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "router_team": {
                        "display_name": "Router Team",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "model_allowlist": ["alpha", "beta", "gamma"],
                        "model_blocklist": ["gamma"],
                        "disabled_models": ["beta"],
                        "models": {
                            "alpha": {"context_window": 32768, "tool_support": True},
                            "beta": {"context_window": 32768, "tool_support": True},
                            "gamma": {"context_window": 32768, "tool_support": True},
                            "delta": {"context_window": 32768, "tool_support": True},
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    catalog = {model.raw_model_ref: model for model in list_model_catalog(cfg)}
    validation = validate_model_selection(cfg, "router_team/beta")
    validate_cli = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path), "--output", "json"])

    assert validate_cli.exit_code == 0, validate_cli.output
    assert "router_team/alpha" in catalog
    assert "router_team/beta" in catalog
    assert "router_team/gamma" not in catalog
    assert "router_team/delta" not in catalog
    assert catalog["router_team/alpha"].status == "active"
    assert catalog["router_team/alpha"].executable_model is True
    assert catalog["router_team/beta"].status == "disabled"
    assert catalog["router_team/beta"].executable_model is False
    assert catalog["router_team/beta"].blocked_reasons == ["model_disabled"]
    assert validation.known_catalog_entry is True
    assert validation.executable is False
    assert validation.blocked_reasons == ["model_disabled"]


def test_custom_provider_requires_boundary_and_endpoint_with_actionable_errors(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "broken_router": {
                        "protocol": "made_up_protocol",
                        "credential": {"kind": "env"},
                        "headers": {"Authorization": {"kind": "literal", "value": "secret-header-value"}},
                        "models": {
                            "bad": {
                                "context_window": 0,
                                "status": "unknown",
                            }
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path), "--output", "json"])
    text_result = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.custom_models_config_validation/v1"
    assert payload["ok"] is False
    assert {
        "provider_data_boundary_missing:broken_router",
        "provider_base_url_missing:broken_router",
        "provider_protocol_invalid:broken_router",
        "credential_env_var_missing:broken_router",
        "header_must_use_env_ref:broken_router:Authorization",
        "model_context_window_invalid:broken_router/bad",
        "model_status_invalid:broken_router/bad",
    }.issubset(set(payload["errors"]))
    issues_by_code = {issue["code"]: issue for issue in payload["validation_issues"]}
    assert issues_by_code["provider_data_boundary_missing"]["path"] == "providers.broken_router.data_boundary"
    assert "local_only" in issues_by_code["provider_data_boundary_missing"]["fix"]
    assert issues_by_code["provider_base_url_missing"]["path"] == "providers.broken_router.base_url"
    assert issues_by_code["provider_protocol_invalid"]["path"] == "providers.broken_router.protocol"
    assert issues_by_code["credential_env_var_missing"]["path"] == "providers.broken_router.credential.env_var"
    assert issues_by_code["header_must_use_env_ref"]["path"] == "providers.broken_router.headers.Authorization"
    assert issues_by_code["model_context_window_invalid"]["path"] == "providers.broken_router.models.bad.context_window"
    assert issues_by_code["model_status_invalid"]["path"] == "providers.broken_router.models.bad.status"
    assert payload["network_accessed"] is False
    assert payload["credentials_included"] is False
    assert "secret-header-value" not in result.output
    assert text_result.exit_code == 1
    assert "Fix:" in text_result.output
    assert "providers.broken_router.data_boundary" in text_result.output
    assert "secret-header-value" not in text_result.output


def test_custom_provider_rejects_missing_endpoint_and_credential_policy(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "missing_policy": {
                        "display_name": "Missing Policy",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "protocol": "openai_chat",
                        "models": {"alpha": {"context_window": 4096}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path), "--output", "json"])
    list_result = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "provider_base_url_missing:missing_policy" in payload["errors"]
    assert "credential_missing:missing_policy" in payload["errors"]
    issues_by_code = {issue["code"]: issue for issue in payload["validation_issues"]}
    assert issues_by_code["provider_base_url_missing"]["path"] == "providers.missing_policy.base_url"
    assert issues_by_code["credential_missing"]["path"] == "providers.missing_policy.credential"
    assert "credential" in issues_by_code["credential_missing"]["fix"]
    assert payload["network_accessed"] is False
    assert payload["credentials_included"] is False
    assert list_result.exit_code == 1
    list_payload = json.loads(list_result.output)
    assert list_payload["errors"] == payload["errors"]
    assert list_payload["validation_issues"] == payload["validation_issues"]


def test_project_custom_models_config_rejects_secret_values_and_unsafe_local_urls(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "unsafe_local": {
                        "data_boundary": "local_only",
                        "base_url": "https://api.example.com/v1",
                        "credential": {"kind": "api_key", "value": "sk-should-not-be-here"},
                        "model_allowlist": "not-a-list",
                        "models": {"bad": {"context_window": 4096}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["models", "config", "validate", "--project", str(tmp_path), "--output", "json"])
    list_result = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.custom_models_config_validation/v1"
    assert payload["ok"] is False
    assert "provider_local_url_not_loopback_or_approved_lan:unsafe_local" in payload["errors"]
    assert "credential_value_not_allowed:unsafe_local:value" in payload["errors"]
    assert "provider_model_allowlist_must_be_string_list:unsafe_local" in payload["errors"]
    issues_by_code = {issue["code"]: issue for issue in payload["validation_issues"]}
    assert issues_by_code["provider_local_url_not_loopback_or_approved_lan"]["path"] == "providers.unsafe_local.base_url"
    assert issues_by_code["credential_value_not_allowed"]["path"] == "providers.unsafe_local.credential.value"
    assert issues_by_code["provider_model_allowlist_must_be_string_list"]["path"] == "providers.unsafe_local.model_allowlist"
    assert "sk-should-not-be-here" not in result.output
    assert list_result.exit_code == 1
    list_payload = json.loads(list_result.output)
    assert list_payload["errors"] == payload["errors"]
    assert list_payload["validation_issues"] == payload["validation_issues"]
    assert "sk-should-not-be-here" not in list_result.output
