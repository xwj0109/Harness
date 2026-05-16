from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.model_catalog import ProviderCredentialStatus, list_model_catalog, list_provider_catalog


runner = CliRunner()


def test_provider_catalog_redacts_credentials_and_marks_disabled_backend() -> None:
    cfg = default_config()
    providers = list_provider_catalog(cfg)
    by_id = {provider.provider_id: provider for provider in providers}

    assert by_id["codex_cli"].credential_status == ProviderCredentialStatus.CONFIGURED
    assert by_id["paid_openai_compatible"].enabled is False
    assert by_id["paid_openai_compatible"].credential_status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["paid_openai_compatible"].constraints

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
    assert providers_payload["permission_granting"] is False
    assert providers_payload["no_hidden_fallback"] is True
    assert providers_payload["cache"]["provider_count"] == len(providers_payload["providers"])
    assert providers_payload["cache"]["permission_granting"] is False
    assert status_payload["schema_version"] == "harness.providers_status/v1"
    assert models_payload["schema_version"] == "harness.models/v1"
    assert models_payload["no_hidden_fallback"] is True
    assert {model["provider_id"] for model in models_payload["models"]} == {"codex_cli"}
    assert "api_key" not in providers.output

    cached = SQLiteStore(tmp_path).list_provider_model_catalog_cache()
    assert {row["catalog_kind"] for row in cached} == {"provider", "model"}
    assert any(row["provider_id"] == "codex_cli" and row["catalog_kind"] == "provider" for row in cached)
    assert any(row["raw_model_ref"] == "codex_cli/gpt-5.5" for row in cached)
    serialized_cache = json.dumps(cached)
    assert "api_key" not in serialized_cache
    assert "ollama" not in serialized_cache


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
