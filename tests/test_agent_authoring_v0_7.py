import json

import pytest

from harness.agent_authoring import (
    AgentBundleError,
    load_agent_bundle,
    preview_agent_bundle,
    scaffold_agent_bundle,
    validate_agent_bundle,
)


def _write_valid_bundle(path, *, agent_id: str = "custom_quant_researcher") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "agent.yaml").write_text(
        f"""
schema_version: harness.agent_bundle/v1
workbench_id: quant
agent:
  id: {agent_id}
  kind: specialist
  role: Custom quant research.
  model_profile: local_reasoning
  tool_policy: read_only
  memory_scope: quant
  parent: quant_research
  outputs:
    - custom_research_note.md
  tags:
    - custom
""".lstrip(),
        encoding="utf-8",
    )
    profiles_dir = path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "default.yaml").write_text(
        f"""
id: {agent_id}.default
agent_id: {agent_id}
description: Default custom profile.
knowledge_domains:
  - commodities
preferred_outputs:
  - custom_research_note.md
review_responsibilities: []
forbidden_actions:
  - live_trading
tags:
  - custom
metadata: {{}}
""".lstrip(),
        encoding="utf-8",
    )


def test_valid_agent_bundle_loads_and_previews_with_profiles(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_valid_bundle(bundle_path)

    loaded = load_agent_bundle(bundle_path)
    validation = validate_agent_bundle(bundle_path)
    preview = preview_agent_bundle(bundle_path)

    assert loaded.bundle.agent.id == "custom_quant_researcher"
    assert validation["schema_version"] == "harness.agent_bundle_validation/v1"
    assert validation["ok"] is True
    assert validation["agent_id"] == "custom_quant_researcher"
    assert [profile["id"] for profile in validation["profiles"]] == ["custom_quant_researcher.default"]
    assert preview["schema_version"] == "harness.agent_bundle_preview/v1"
    assert preview["ok"] is True
    assert preview["agent"]["id"] == "custom_quant_researcher"
    assert [parent["id"] for parent in preview["parent_chain"]] == ["quant_research"]
    assert preview["effective_agent"]["parent_chain"] == ["quant_research"]
    assert preview["workbench"]["id"] == "quant"


def test_agent_bundle_without_profiles_validates_and_previews(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_valid_bundle(bundle_path)
    for path in (bundle_path / "profiles").glob("*.yaml"):
        path.unlink()

    validation = validate_agent_bundle(bundle_path)
    preview = preview_agent_bundle(bundle_path)

    assert validation["ok"] is True
    assert validation["profiles"] == []
    assert preview["ok"] is True
    assert preview["profiles"] == []


def test_scaffold_creates_deterministic_agent_bundle(tmp_path) -> None:
    destination = tmp_path / "agents" / "my_agent"

    result = scaffold_agent_bundle(
        agent_id="my_agent",
        workbench_id="quant",
        kind="specialist",
        parent="quant_research",
        model_profile="local_reasoning",
        tool_policy="read_only",
        memory_scope="quant",
        output_path=destination,
        role="My custom agent.",
    )

    assert result["schema_version"] == "harness.agent_scaffold/v1"
    assert result["ok"] is True
    assert (destination / "agent.yaml").exists()
    assert (destination / "profiles" / "default.yaml").exists()
    validation = validate_agent_bundle(destination)
    assert validation["ok"] is True
    assert validation["agent_id"] == "my_agent"


def test_agent_bundle_missing_agent_yaml_returns_stable_error(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "missing"
    bundle_path.mkdir(parents=True)

    result = validate_agent_bundle(bundle_path)

    assert result["ok"] is False
    assert result["errors"] == [f"Agent bundle missing agent.yaml: {bundle_path.resolve()}"]


def test_agent_bundle_malformed_yaml_returns_stable_error(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "bad"
    bundle_path.mkdir(parents=True)
    (bundle_path / "agent.yaml").write_text("agent: [unterminated\n", encoding="utf-8")

    result = validate_agent_bundle(bundle_path)

    assert result["ok"] is False
    assert "could not be parsed" in result["errors"][0]


def test_agent_bundle_rejects_builtin_shadowing_and_duplicate_profiles(tmp_path) -> None:
    shadow_path = tmp_path / "agents" / "shadow"
    _write_valid_bundle(shadow_path, agent_id="repo_inspector")
    duplicate_path = tmp_path / "agents" / "duplicate"
    _write_valid_bundle(duplicate_path, agent_id="custom_quant_researcher")
    (duplicate_path / "profiles" / "other.yaml").write_text(
        (duplicate_path / "profiles" / "default.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    shadow = validate_agent_bundle(shadow_path)
    duplicate = validate_agent_bundle(duplicate_path)

    assert shadow["ok"] is False
    assert shadow["errors"] == ["Custom agent id shadows built-in agent: repo_inspector"]
    assert duplicate["ok"] is False
    assert duplicate["errors"] == ["Duplicate custom agent profile id."]


def test_agent_bundle_rejects_profile_for_other_agent_and_builtin_profile_shadow(tmp_path) -> None:
    wrong_profile = tmp_path / "agents" / "wrong_profile"
    _write_valid_bundle(wrong_profile)
    (wrong_profile / "profiles" / "default.yaml").write_text(
        """
id: custom_quant_researcher.default
agent_id: other_agent
description: Wrong profile.
""".lstrip(),
        encoding="utf-8",
    )
    shadow_profile = tmp_path / "agents" / "shadow_profile"
    _write_valid_bundle(shadow_profile)
    (shadow_profile / "profiles" / "default.yaml").write_text(
        """
id: commodities_researcher.default
agent_id: custom_quant_researcher
description: Shadow profile.
""".lstrip(),
        encoding="utf-8",
    )

    wrong = validate_agent_bundle(wrong_profile)
    shadow = validate_agent_bundle(shadow_profile)

    assert wrong["ok"] is False
    assert wrong["errors"] == ["Agent profile custom_quant_researcher.default must reference bundle agent: custom_quant_researcher"]
    assert shadow["ok"] is False
    assert shadow["errors"] == ["Custom agent profile id shadows built-in profile: commodities_researcher.default"]


def test_agent_bundle_rejects_unknown_references_and_policy_broadening(tmp_path) -> None:
    missing = tmp_path / "agents" / "missing_ref"
    _write_valid_bundle(missing)
    agent_yaml = (missing / "agent.yaml").read_text(encoding="utf-8")
    (missing / "agent.yaml").write_text(agent_yaml.replace("model_profile: local_reasoning", "model_profile: missing"), encoding="utf-8")
    broadening = tmp_path / "agents" / "broadening"
    _write_valid_bundle(broadening)
    agent_yaml = (broadening / "agent.yaml").read_text(encoding="utf-8")
    (broadening / "agent.yaml").write_text(agent_yaml.replace("tool_policy: read_only", "tool_policy: isolated_code_edit"), encoding="utf-8")

    missing_result = validate_agent_bundle(missing)
    broadening_result = validate_agent_bundle(broadening)

    assert missing_result["ok"] is False
    assert "missing model_profile" in missing_result["errors"][0]
    assert broadening_result["ok"] is False
    assert "broadens parent quant_research active_repo_write" in broadening_result["errors"][0]


def test_agent_bundle_rejects_forbidden_paths_and_existing_non_empty_scaffold_destination(tmp_path) -> None:
    forbidden = tmp_path / ".harness" / "agent"
    result = validate_agent_bundle(forbidden)
    destination = tmp_path / "agents" / "occupied"
    destination.mkdir(parents=True)
    (destination / "README.md").write_text("occupied", encoding="utf-8")

    assert result["ok"] is False
    assert result["errors"] == ["Agent bundle path is forbidden by harness safety policy."]
    with pytest.raises(AgentBundleError, match="destination is not empty"):
        scaffold_agent_bundle(
            agent_id="my_agent",
            workbench_id="quant",
            kind="specialist",
            parent="quant_research",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="quant",
            output_path=destination,
        )


def test_agent_bundle_rejects_symlink_paths(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_valid_bundle(bundle_path)
    symlink_path = tmp_path / "agent_link"
    symlink_path.symlink_to(bundle_path, target_is_directory=True)
    output_target = tmp_path / "output_target"
    output_target.mkdir()
    output_symlink = tmp_path / "output_link"
    output_symlink.symlink_to(output_target, target_is_directory=True)

    result = validate_agent_bundle(symlink_path)

    assert result["ok"] is False
    assert result["errors"] == ["Agent bundle path cannot include symlinks."]
    with pytest.raises(AgentBundleError, match="cannot include symlinks"):
        scaffold_agent_bundle(
            agent_id="my_agent",
            workbench_id="quant",
            kind="specialist",
            parent="quant_research",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="quant",
            output_path=output_symlink / "my_agent",
        )


def test_agent_bundle_rejects_unsupported_profile_files_and_profile_directories(tmp_path) -> None:
    unsupported = tmp_path / "agents" / "unsupported"
    _write_valid_bundle(unsupported)
    (unsupported / "profiles" / "notes.txt").write_text("not a profile", encoding="utf-8")
    directory_entry = tmp_path / "agents" / "directory_entry"
    _write_valid_bundle(directory_entry)
    (directory_entry / "profiles" / "nested.yaml").mkdir()

    unsupported_result = validate_agent_bundle(unsupported)
    directory_result = validate_agent_bundle(directory_entry)

    assert unsupported_result["ok"] is False
    assert unsupported_result["errors"] == ["Unsupported agent profile extension: .txt"]
    assert directory_result["ok"] is False
    assert "Agent profile entry is not a file" in directory_result["errors"][0]


def test_agent_scaffold_rejects_existing_file_destination_and_invalid_kind(tmp_path) -> None:
    existing_file = tmp_path / "agents" / "file_agent"
    existing_file.parent.mkdir()
    existing_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(AgentBundleError, match="destination is not a directory"):
        scaffold_agent_bundle(
            agent_id="my_agent",
            workbench_id="quant",
            kind="specialist",
            parent="quant_research",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="quant",
            output_path=existing_file,
        )
    with pytest.raises(AgentBundleError, match="Input should be"):
        scaffold_agent_bundle(
            agent_id="my_agent",
            workbench_id="quant",
            kind="unsupported",
            parent="quant_research",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="quant",
            output_path=tmp_path / "agents" / "invalid_kind",
        )


def test_agent_bundle_preview_output_contains_no_secret_like_terms(tmp_path) -> None:
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_valid_bundle(bundle_path)

    payload = json.dumps(preview_agent_bundle(bundle_path))

    assert "api_key" not in payload
    assert "OPENAI_API_KEY" not in payload
    assert "base_url" not in payload
