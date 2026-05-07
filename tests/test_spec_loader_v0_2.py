import json

import pytest

from harness.registry import SpecRegistry
from harness.spec_loader import load_spec_registry, resolve_spec_bundle_path, validate_spec_bundle


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
        "memory_scopes": {
            "project": {
                "id": "project",
                "description": "Project scope.",
            }
        },
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


def test_valid_json_bundle_loads_into_spec_registry(tmp_path) -> None:
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(_valid_bundle()), encoding="utf-8")

    registry = load_spec_registry(path)

    assert isinstance(registry, SpecRegistry)
    assert registry.get_agent("repo_inspector").model_profile == "local_reasoning"
    assert registry.get_workbench("coding").allowed_agents == ["repo_inspector"]
    assert not hasattr(registry, "schema_version")


def test_valid_yaml_bundle_loads_into_spec_registry(tmp_path) -> None:
    path = tmp_path / "specs.yaml"
    path.write_text(
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

    registry = load_spec_registry(path)

    assert registry.get_agent("repo_inspector").tool_policy == "read_only"
    assert registry.get_workbench("coding").default_model_profile == "local_reasoning"


def test_missing_schema_version_returns_clear_validation_error(tmp_path) -> None:
    bundle = _valid_bundle()
    del bundle["schema_version"]
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert result["errors"] == ["Spec bundle missing schema_version."]


def test_unsupported_schema_version_returns_clear_validation_error(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["schema_version"] = "harness.spec_bundle/v0"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert result["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]


def test_invalid_json_returns_clear_validation_error(tmp_path) -> None:
    path = tmp_path / "specs.json"
    path.write_text("{not valid json", encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "could not be parsed" in result["errors"][0]


def test_invalid_yaml_returns_clear_validation_error(tmp_path) -> None:
    path = tmp_path / "specs.yaml"
    path.write_text("model_profiles: [unterminated\n", encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "could not be parsed" in result["errors"][0]


def test_missing_agent_reference_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["agents"]["repo_inspector"]["model_profile"] = "missing_profile"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "missing model_profile" in result["errors"][0]


def test_missing_workbench_agent_reference_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["workbenches"]["coding"]["allowed_agents"] = ["missing_agent"]
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "missing allowed agent" in result["errors"][0]


def test_mapping_key_mismatch_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["agents"] = {
        "inspector": {
            "id": "repo_inspector",
            "kind": "specialist",
            "role": "Inspect repository evidence.",
            "model_profile": "local_reasoning",
            "tool_policy": "read_only",
            "memory_scope": "project",
        }
    }
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "agent mapping key must match contained id" in result["errors"][0]


def test_forbidden_memory_scope_allowed_path_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["memory_scopes"]["project"]["allowed_paths"] = [".harness/runs"]
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "MemoryScope allowed_paths cannot include repository hard-forbidden path: .harness/runs" in result[
        "errors"
    ][0]


def test_invalid_model_profile_backend_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["model_profiles"]["local_reasoning"]["backend"] = "codex_cli"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "Local model profile backend is not local-compatible: codex_cli" in result["errors"][0]


def test_forbidden_model_profile_constraint_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["model_profiles"]["codex_supervised"] = {
        "id": "codex_supervised",
        "kind": "external_agent",
        "backend": "codex_cli",
        "constraints": ["supervised_external_agent", "hosted_fallback"],
    }
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "Model profile constraint is forbidden by harness safety policy: hosted_fallback" in result["errors"][0]


def test_forbidden_tool_policy_network_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["tool_policies"]["read_only"]["network"] = "allowed"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "ToolPolicy network cannot be allowed." in result["errors"][0]


def test_forbidden_tool_policy_active_repo_write_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["tool_policies"]["read_only"]["active_repo_write"] = "allowed"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "ToolPolicy active_repo_write cannot be allowed." in result["errors"][0]


def test_coding_workbench_missing_required_forbidden_actions_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["workbenches"]["coding"]["forbidden_actions"] = []
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "Workbench coding forbidden_actions missing required actions: hosted_fallback, paid_api_fallback" in result[
        "errors"
    ][0]


def test_quant_workbench_missing_required_forbidden_action_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["agents"]["quant_researcher"] = {
        "id": "quant_researcher",
        "kind": "specialist",
        "role": "Draft quant research notes.",
        "model_profile": "local_reasoning",
        "tool_policy": "read_only",
        "memory_scope": "project",
    }
    bundle["workbenches"] = {
        "quant": {
            "id": "quant",
            "description": "Quant workbench.",
            "allowed_agents": ["quant_researcher"],
            "default_model_profile": "local_reasoning",
            "forbidden_actions": ["live_trading", "capital_allocation"],
        }
    }
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "Workbench quant forbidden_actions missing required actions: broker_action" in result["errors"][0]


def test_personal_workbench_missing_required_forbidden_action_fails_through_registry_validation(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["agents"]["job_researcher"] = {
        "id": "job_researcher",
        "kind": "specialist",
        "role": "Draft job research notes.",
        "model_profile": "local_reasoning",
        "tool_policy": "read_only",
        "memory_scope": "project",
    }
    bundle["workbenches"] = {
        "personal": {
            "id": "personal",
            "description": "Personal workbench.",
            "allowed_agents": ["job_researcher"],
            "default_model_profile": "local_reasoning",
            "forbidden_actions": ["email_send", "application_submit"],
        }
    }
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert "Workbench personal forbidden_actions missing required actions: external_message_send" in result[
        "errors"
    ][0]


def test_unsupported_extension_fails_clearly(tmp_path) -> None:
    path = tmp_path / "specs.txt"
    path.write_text(json.dumps(_valid_bundle()), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "Unsupported spec bundle extension: .txt" in result["errors"][0]


@pytest.mark.parametrize(
    "relative_path",
    [
        ".harness/specs.json",
        ".git/specs.yaml",
        "secrets/specs.yml",
        ".env",
        ".env.json",
        ".env.local.yaml",
        "private.pem",
        "private.key",
        "state.sqlite",
    ],
)
def test_explicit_spec_bundle_path_guard_rejects_forbidden_paths_before_reading(tmp_path, relative_path) -> None:
    path = tmp_path / relative_path

    result = validate_spec_bundle(path)

    assert result["schema_version"] == "harness.spec_validation/v1"
    assert result["ok"] is False
    assert result["errors"] == ["Spec bundle path is forbidden by harness safety policy."]
    assert not (tmp_path / ".harness").exists()
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / "secrets").exists()


def test_secret_like_explicit_path_is_rejected_before_reading(tmp_path) -> None:
    path = tmp_path / ".env.json"
    path.write_text(json.dumps(_valid_bundle()), encoding="utf-8")

    result = validate_spec_bundle(path)

    assert result["ok"] is False
    assert "forbidden by harness safety policy" in result["errors"][0]


def test_resolve_spec_bundle_path_returns_valid_explicit_bundle_path(tmp_path) -> None:
    path = tmp_path / "specs.yml"
    path.write_text(json.dumps(_valid_bundle()), encoding="utf-8")

    assert resolve_spec_bundle_path(path) == path.resolve()
