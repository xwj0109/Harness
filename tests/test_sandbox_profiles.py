import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.execution import list_execution_adapter_descriptors
from harness.sandbox_profiles import get_sandbox_profile, list_sandbox_profiles


runner = CliRunner()


def test_builtin_sandbox_profiles_are_stable_and_unique() -> None:
    profiles = list_sandbox_profiles()
    ids = [profile.id for profile in profiles]

    assert ids == ["none", "read_only_codex", "isolated_workspace_codex", "docker_test_sandbox"]
    assert len(ids) == len(set(ids))
    assert all(profile.schema_version == "harness.sandbox_profile/v1" for profile in profiles)
    assert get_sandbox_profile("none").tier.value == "none"


def test_registered_adapters_have_valid_sandbox_profiles() -> None:
    profiles = {profile.id for profile in list_sandbox_profiles()}
    by_id = {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}

    assert all(descriptor.sandbox_profile_id in profiles for descriptor in by_id.values())
    assert by_id["dry_run"].sandbox_profile_id == "none"
    assert by_id["read_only_summary"].sandbox_profile_id == "read_only_codex"
    assert by_id["repo_planning"].sandbox_profile_id == "read_only_codex"
    assert by_id["codex_isolated_edit"].sandbox_profile_id == "isolated_workspace_codex"


def test_sandbox_profile_cli_is_read_only_without_init(tmp_path, monkeypatch) -> None:
    def fail_backend(*_args, **_kwargs):
        raise AssertionError("sandbox profile commands must not preflight backends or Docker")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.DockerImageManager", fail_backend)

    listed = runner.invoke(app, ["sandbox", "profiles", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(app, ["sandbox", "inspect", "none", "--project", str(tmp_path), "--output", "json"])
    missing = runner.invoke(app, ["sandbox", "inspect", "missing", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.sandbox_profiles/v1"
    assert {profile["id"] for profile in listed_payload["profiles"]} == {
        "none",
        "read_only_codex",
        "isolated_workspace_codex",
        "docker_test_sandbox",
    }
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.sandbox_profile/v1"
    assert inspected_payload["id"] == "none"
    assert inspected_payload["tier"] == "none"
    assert missing.exit_code == 1
    missing_payload = json.loads(missing.output)
    assert missing_payload["schema_version"] == "harness.sandbox_profile/v1"
    assert missing_payload["ok"] is False
    assert missing_payload["errors"] == ["Sandbox profile not found: missing"]
    assert not (tmp_path / ".harness").exists()
