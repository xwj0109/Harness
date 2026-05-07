import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.registry import builtin_spec_registry
from harness.spec_loader import export_builtin_spec_registry


runner = CliRunner()


def _builtin_bundle() -> dict:
    registry = export_builtin_spec_registry(builtin_spec_registry())["registry"]
    return {"schema_version": "harness.spec_bundle/v1", **registry}


def test_cli_specs_preview_builtin_agent_json_resolves_references(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["specs", "preview", "agent", "repo_inspector", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert payload["source"] == {"kind": "builtin", "path": None}
    assert payload["target"] == {"kind": "agent", "id": "repo_inspector"}
    assert payload["preview"]["agent"]["id"] == "repo_inspector"
    assert payload["preview"]["model_profile"]["id"] == "local_reasoning"
    assert payload["preview"]["tool_policy"]["active_repo_write"] == "forbidden"
    assert payload["preview"]["memory_scope"]["id"] == "project"
    assert payload["preview"]["parent"] is None
    assert payload["preview"]["parent_chain"] == []
    assert payload["preview"]["effective_agent"]["parent_chain"] == []
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_builtin_grouped_quant_agent_resolves_parent_chain(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["specs", "preview", "agent", "commodities_researcher", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    preview = payload["preview"]
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert preview["agent"]["id"] == "commodities_researcher"
    assert preview["parent"] == "quant_research"
    assert [parent["id"] for parent in preview["parent_chain"]] == ["quant_research"]
    assert preview["effective_agent"]["parent_chain"] == ["quant_research"]
    assert preview["effective_agent"]["model_profile"] == "local_reasoning"
    assert preview["effective_agent"]["tool_policy"] == "read_only"
    assert preview["effective_agent"]["memory_scope"] == "quant"
    assert preview["effective_agent"]["tags"] == ["starter", "quant", "group", "research", "commodities"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_builtin_workbench_json_resolves_allowed_agents(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["specs", "preview", "workbench", "coding", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    preview = payload["preview"]
    assert preview["workbench"]["id"] == "coding"
    assert preview["default_model_profile"]["id"] == "local_reasoning"
    assert list(preview["allowed_agents"]) == ["code_editor", "repo_inspector", "test_runner"]
    assert preview["allowed_agents"]["repo_inspector"]["tool_policy"]["network"] == "forbidden"
    assert preview["allowed_agents"]["repo_inspector"]["parent_chain"] == []
    assert preview["forbidden_actions"] == ["hosted_fallback", "paid_api_fallback"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_custom_json_agent(tmp_path) -> None:
    bundle = _builtin_bundle()
    bundle["agents"]["repo_inspector"]["role"] = "Custom repo inspection."
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = runner.invoke(
        app,
        ["specs", "preview", "agent", "repo_inspector", "--source", str(bundle_path), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert payload["preview"]["agent"]["role"] == "Custom repo inspection."
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_custom_yaml_workbench(tmp_path) -> None:
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

    result = runner.invoke(
        app,
        ["specs", "preview", "workbench", "coding", "--source", str(bundle_path), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert list(payload["preview"]["allowed_agents"]) == ["repo_inspector"]
    assert payload["preview"]["allowed_agents"]["repo_inspector"]["model_profile"]["id"] == "local_reasoning"
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_missing_agent_fails_json(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["specs", "preview", "agent", "missing_agent", "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert payload["ok"] is False
    assert payload["source"] == {"kind": "builtin", "path": None}
    assert payload["target"] == {"kind": "agent", "id": "missing_agent"}
    assert payload["errors"] == ["Agent not found: missing_agent"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_missing_workbench_fails_json(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["specs", "preview", "workbench", "missing_workbench", "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert payload["ok"] is False
    assert payload["target"] == {"kind": "workbench", "id": "missing_workbench"}
    assert payload["errors"] == ["Workbench not found: missing_workbench"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_invalid_custom_bundle_fails_json(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps({"schema_version": "harness.spec_bundle/v0"}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["specs", "preview", "agent", "repo_inspector", "--source", str(bundle_path), "--output", "json"],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert payload["ok"] is False
    assert payload["source"] == {"kind": "custom", "path": str(bundle_path.resolve())}
    assert payload["target"] == {"kind": "agent", "id": "repo_inspector"}
    assert payload["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_preview_does_not_load_project_state_or_preflight_backends(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec preview must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec preview must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec preview must not preflight local backend")),
    )

    result = runner.invoke(app, ["specs", "preview", "agent", "repo_inspector", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_effective_preview/v1"
    assert not (tmp_path / ".harness").exists()
