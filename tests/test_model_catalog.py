from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.model_catalog import ProviderCredentialStatus, list_model_catalog, list_provider_catalog, validate_model_selection


runner = CliRunner()


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
    assert "OPENAI_API_KEY" in serialized
    assert "silently falling back" in serialized.lower()


def test_model_catalog_lists_backend_and_profile_model_refs_without_fallback() -> None:
    cfg = default_config()
    models = list_model_catalog(cfg)
    refs = {(model.provider_id, model.model_profile_id, model.raw_model_ref) for model in models}

    assert ("codex_cli", None, "codex_cli/gpt-5.5") in refs
    assert ("local_openai_compatible", None, "local_openai_compatible/qwen3-coder:30b") in refs
    assert ("codex_cli", "codex_supervised", "codex_cli/gpt-5.5") in refs
    assert ("local_openai_compatible", "local_reasoning", "local_openai_compatible/qwen3-coder:30b") in refs
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
    disabled = validate_model_selection(cfg, "paid_openai_compatible/gpt-5.3-codex")
    unknown_provider = validate_model_selection(cfg, "missing/gpt-5.5")
    unknown_model = validate_model_selection(cfg, "codex_cli/not-a-real-model")
    unspecified_provider = validate_model_selection(cfg, "gpt-5.5")
    missing = validate_model_selection(cfg, None)

    assert known.schema_version == "harness.model_selection_validation/v1"
    assert known.known_catalog_entry is True
    assert known.provider_known is True
    assert known.provider_enabled is True
    assert known.executable is True
    assert known.blocked_reasons == []
    assert known.matched_model is not None
    assert known.matched_model.raw_model_ref == "codex_cli/gpt-5.5"
    assert known.policy_boundary == {
        "kind": "model_selection_validation",
        "source": "provider_model_catalog",
        "metadata_only": True,
    }
    for result in (known, disabled, unknown_provider, unknown_model, unspecified_provider, missing):
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
    assert disabled.blocked_reasons == ["provider_disabled"]
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


def test_provider_and_model_catalog_cli_json_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    providers = runner.invoke(app, ["providers", "list", "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["providers", "status", "--project", str(tmp_path), "--output", "json"])
    models = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--provider", "codex_cli", "--output", "json"])

    assert providers.exit_code == 0, providers.output
    assert status.exit_code == 0, status.output
    assert models.exit_code == 0, models.output

    providers_payload = json.loads(providers.output)
    status_payload = json.loads(status.output)
    models_payload = json.loads(models.output)
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
    assert validation.exit_code == 0, validation.output
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
    assert model_payload["models"][0]["provider_execution_started"] is False
    assert model_payload["models"][0]["network_accessed"] is False
    assert model_payload["models"][0]["credentials_included"] is False
    assert json.loads(validation.output)["validation"]["executable"] is True
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


def test_models_refresh_fails_closed_without_provider_network_call(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--refresh", "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "refusing to call providers implicitly" in payload["error"]


def test_provider_login_logout_fail_closed_without_credential_side_effects(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    before = SQLiteStore(tmp_path).list_provider_model_catalog_cache()

    login = runner.invoke(app, ["providers", "login", "codex_cli", "--project", str(tmp_path), "--output", "json"])
    logout = runner.invoke(app, ["providers", "logout", "codex_cli", "--project", str(tmp_path), "--output", "json"])
    missing = runner.invoke(app, ["providers", "login", "missing", "--project", str(tmp_path), "--output", "json"])

    assert login.exit_code == 1
    assert logout.exit_code == 1
    assert missing.exit_code == 1
    login_payload = json.loads(login.output)
    logout_payload = json.loads(logout.output)
    missing_payload = json.loads(missing.output)
    assert login_payload["schema_version"] == "harness.provider_auth/v1"
    assert login_payload["action"] == "login"
    assert logout_payload["action"] == "logout"
    assert login_payload["permission_granting"] is False
    assert logout_payload["no_hidden_fallback"] is True
    assert "refusing to write credentials" in login_payload["error"]
    assert "refusing to remove credentials" in logout_payload["error"]
    assert missing_payload["ok"] is False
    assert "Provider not found: missing" in missing_payload["error"]
    assert SQLiteStore(tmp_path).list_provider_model_catalog_cache() == before
    assert "api_key" not in login.output
    assert "api_key" not in logout.output
