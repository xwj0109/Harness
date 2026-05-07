import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.registry import builtin_spec_registry
from harness.spec_loader import export_builtin_spec_registry


runner = CliRunner()


def _builtin_bundle() -> dict:
    registry = export_builtin_spec_registry(builtin_spec_registry())["registry"]
    return {"schema_version": "harness.spec_bundle/v1", **registry}


def test_cli_specs_diff_json_reports_added_removed_changed_and_unchanged(tmp_path) -> None:
    bundle = _builtin_bundle()
    bundle["memory_scopes"]["archive"] = {
        "id": "archive",
        "description": "Archive memory scope.",
    }
    bundle["memory_scopes"]["project"]["description"] = "Custom project memory scope."
    del bundle["workbenches"]["personal"]
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = runner.invoke(app, ["specs", "diff", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_diff/v1"
    assert payload["source"] == {
        "base": {"kind": "builtin", "path": None},
        "compare": {"kind": "custom", "path": str(bundle_path.resolve())},
    }
    assert payload["diff"]["memory_scopes"]["added"] == ["archive"]
    assert payload["diff"]["memory_scopes"]["changed"] == ["project"]
    assert payload["diff"]["workbenches"]["removed"] == ["personal"]
    assert "local_reasoning" in payload["diff"]["model_profiles"]["unchanged"]
    assert "repo_inspector" in payload["diff"]["agents"]["unchanged"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_diff_json_sorts_sections_and_ids(tmp_path) -> None:
    bundle = _builtin_bundle()
    bundle["memory_scopes"]["z_archive"] = {"id": "z_archive"}
    bundle["memory_scopes"]["a_archive"] = {"id": "a_archive"}
    bundle["workbenches"]["coding"]["description"] = "Custom coding workbench."
    bundle["workbenches"]["quant"]["description"] = "Custom quant workbench."
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = runner.invoke(app, ["specs", "diff", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert list(payload["diff"]) == sorted(payload["diff"])
    assert payload["diff"]["memory_scopes"]["added"] == ["a_archive", "z_archive"]
    assert payload["diff"]["workbenches"]["changed"] == ["coding", "quant"]


def test_cli_specs_diff_invalid_custom_bundle_fails_json(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps({"schema_version": "harness.spec_bundle/v0"}), encoding="utf-8")

    result = runner.invoke(app, ["specs", "diff", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_diff/v1"
    assert payload["ok"] is False
    assert payload["source"] == {
        "base": {"kind": "builtin", "path": None},
        "compare": {"kind": "custom", "path": str(bundle_path.resolve())},
    }
    assert payload["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_diff_does_not_load_project_state_or_preflight_backends(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec diff must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec diff must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec diff must not preflight local backend")),
    )
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(json.dumps(_builtin_bundle()), encoding="utf-8")

    result = runner.invoke(app, ["specs", "diff", "--source", str(bundle_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_diff/v1"
    assert not (tmp_path / ".harness").exists()
