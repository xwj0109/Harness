import json

from typer.testing import CliRunner

from harness.cli.main import app


runner = CliRunner()


def _valid_bundle() -> dict:
    return {
        "schema_version": "harness.spec_bundle/v1",
        "model_profiles": {
            "local_reasoning": {
                "id": "local_reasoning",
                "kind": "local",
                "backend": "local_openai_compatible",
            }
        },
        "tool_policies": {
            "read_only": {
                "tools": {"repo_read": "allowed"},
                "network": "forbidden",
                "active_repo_write": "forbidden",
                "hosted_boundary": "approval_required",
            }
        },
        "memory_scopes": {"project": {"id": "project"}},
        "agents": {
            "repo_inspector": {
                "id": "repo_inspector",
                "kind": "specialist",
                "role": "Inspect repository evidence.",
                "model_profile": "local_reasoning",
                "tool_policy": "read_only",
                "memory_scope": "project",
            }
        },
        "workbenches": {
            "coding": {
                "id": "coding",
                "description": "Coding workbench.",
                "allowed_agents": ["repo_inspector"],
                "default_model_profile": "local_reasoning",
                "forbidden_actions": ["paid_api_fallback", "hosted_fallback"],
            }
        },
    }


def test_cli_specs_export_builtin_json_is_stable_without_project_state(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec export must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec export must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec export must not preflight local backend")),
    )

    result = runner.invoke(app, ["specs", "export", "--source", "builtin", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_export/v1"
    assert payload["source"] == {"kind": "builtin", "path": None}
    assert set(payload["registry"]) == {
        "agent_profiles",
        "model_profiles",
        "tool_policies",
        "memory_scopes",
        "agents",
        "workbenches",
    }
    assert list(payload["registry"]["agents"]) == sorted(payload["registry"]["agents"])
    assert "repo_inspector" in payload["registry"]["agents"]
    assert not (tmp_path / ".harness").exists()
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_specs_export_custom_json_bundle(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps(_valid_bundle()), encoding="utf-8")

    result = runner.invoke(app, ["specs", "export", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_export/v1"
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert payload["registry"]["agents"]["repo_inspector"]["kind"] == "specialist"
    assert list(payload["registry"]["model_profiles"]) == ["local_reasoning"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_export_custom_yaml_bundle(tmp_path) -> None:
    bundle_path = tmp_path / "specs.yaml"
    bundle_path.write_text(
        """
schema_version: harness.spec_bundle/v1
model_profiles:
  local_reasoning:
    id: local_reasoning
    kind: local
    backend: local_openai_compatible
tool_policies:
  read_only:
    tools:
      repo_read: allowed
    network: forbidden
    active_repo_write: forbidden
    hosted_boundary: approval_required
memory_scopes:
  project:
    id: project
agents:
  repo_inspector:
    id: repo_inspector
    kind: specialist
    role: Inspect repository evidence.
    model_profile: local_reasoning
    tool_policy: read_only
    memory_scope: project
workbenches:
  coding:
    id: coding
    description: Coding workbench.
    allowed_agents:
      - repo_inspector
    default_model_profile: local_reasoning
    forbidden_actions:
      - paid_api_fallback
      - hosted_fallback
""".lstrip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["specs", "export", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert payload["registry"]["workbenches"]["coding"]["forbidden_actions"] == [
        "paid_api_fallback",
        "hosted_fallback",
    ]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_export_invalid_custom_bundle_fails_json(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps({"schema_version": "harness.spec_bundle/v0"}), encoding="utf-8")

    result = runner.invoke(app, ["specs", "export", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_export/v1"
    assert payload["ok"] is False
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert payload["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]
    assert not (tmp_path / ".harness").exists()
