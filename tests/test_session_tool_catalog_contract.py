from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from harness.cli.main import app
from harness.session_tools import HARNESS_SESSION_TOOL_IDS, session_tool_catalog_projection


runner = CliRunner()

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "session_tool_catalog" / "catalog_policy_golden.json"
REPRESENTATIVE_TOOL_IDS = [
    "read",
    "plan-enter",
    "write",
    "patch",
    "shell",
    "web-fetch",
    "web-search",
    "mcp",
    "mcp-resource",
    "plugin-tool",
    "skill-load",
    "task",
]
POLICY_CONTRACT_FIELDS = [
    "boundary_kind",
    "disabled_reason",
    "enabled",
    "exposure",
    "execution_supported",
    "maturity",
    "permission_key",
    "permission_required",
    "planning_only",
    "policy_reasons",
    "policy_source",
    "replay_policy",
    "required_client_capability",
    "required_config",
    "required_model_capability",
    "risk",
    "schema_version",
    "tool_id",
]
REPRESENTATIVE_POLICY_FIELDS = [
    "boundary_kind",
    "disabled_reason",
    "enabled",
    "execution_supported",
    "maturity",
    "permission_key",
    "permission_required",
    "planning_only",
    "replay_policy",
    "required_client_capability",
    "required_config",
    "required_model_capability",
    "risk",
]


def _init_project(project_root: Path) -> None:
    result = runner.invoke(app, ["init", "--project", str(project_root)])
    assert result.exit_code == 0, result.output


def _update_config(project_root: Path, updates: dict[str, Any]) -> None:
    config_path = project_root / ".harness" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _deep_update(config, updates)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _catalog_payload(project_root: Path) -> dict[str, Any]:
    return session_tool_catalog_projection(project_root=project_root)


def _representative_summary(payload: dict[str, Any], representative_ids: list[str]) -> dict[str, Any]:
    tools = payload["tools"]
    by_id = {tool["id"]: tool for tool in tools}
    return {
        tool_id: {
            "descriptor_enabled": by_id[tool_id]["enabled"],
            "tool_class": by_id[tool_id]["tool_class"],
            "policy": {field: by_id[tool_id]["policy"][field] for field in REPRESENTATIVE_POLICY_FIELDS},
        }
        for tool_id in representative_ids
    }


def _catalog_contract_summary(project_root: Path) -> dict[str, Any]:
    payload = session_tool_catalog_projection(project_root=project_root)
    tools = payload["tools"]
    first_policy = tools[0]["policy"]
    return {
        "schema_version": payload["schema_version"],
        "policy_projection_schema_version": payload["policy_projection_schema_version"],
        "policy_fields": sorted(first_policy),
        "tool_ids": [tool["id"] for tool in tools],
        "representative_tools": _representative_summary(payload, REPRESENTATIVE_TOOL_IDS),
    }


def _build_catalog_cases(tmp_path: Path) -> dict[str, Any]:
    cases: dict[str, Any] = {}

    default_project = tmp_path / "default"
    _init_project(default_project)
    cases["default"] = _catalog_contract_summary(default_project)

    web_project = tmp_path / "web_enabled"
    _init_project(web_project)
    _update_config(
        web_project,
        {
            "web_tools": {
                "enabled": True,
                "fetch_enabled": True,
                "search_enabled": True,
                "approval_required": True,
                "allowed_domains": ["docs.example.com"],
                "search_provider": "configured_http",
                "search_endpoint_url": "http://127.0.0.1:7777/search",
            }
        },
    )
    cases["web_enabled"] = _representative_summary(_catalog_payload(web_project), ["web-fetch", "web-search"])

    mcp_project = tmp_path / "mcp_resource"
    _init_project(mcp_project)
    _update_config(
        mcp_project,
        {
            "mcp": {
                "enabled": True,
                "servers": {
                    "docs": {
                        "kind": "local",
                        "enabled": False,
                        "command": ["docs-mcp"],
                        "resources": {
                            "guide": {
                                "uri": "mcp://docs/guide",
                                "path": "docs/guide.md",
                                "description": "Cached guide",
                            }
                        },
                    }
                },
            }
        },
    )
    cases["mcp_resource"] = _representative_summary(_catalog_payload(mcp_project), ["mcp", "mcp-resource"])

    skill_project = tmp_path / "skill_enabled"
    _init_project(skill_project)
    _update_config(
        skill_project,
        {
            "skills": {
                "enabled": True,
                "project": {
                    "reviewer": {
                        "path": "skills/reviewer",
                        "enabled": True,
                        "description": "Project review skill",
                        "version": "0.1.0",
                    }
                },
            }
        },
    )
    cases["skill_enabled"] = _representative_summary(_catalog_payload(skill_project), ["skill-load"])

    return {"schema_version": "harness.session_tool_catalog_golden/v1", "cases": cases}


def test_session_tool_catalog_golden_contract(tmp_path) -> None:
    expected = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    actual = _build_catalog_cases(tmp_path)

    assert actual == expected
    assert actual["cases"]["default"]["tool_ids"] == HARNESS_SESSION_TOOL_IDS


def test_session_tool_catalog_docs_cover_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs" / "session_tool_catalog.md").read_text(encoding="utf-8")

    for tool_id in HARNESS_SESSION_TOOL_IDS:
        assert f"`{tool_id}`" in docs
    for field in POLICY_CONTRACT_FIELDS:
        assert f"`{field}`" in docs
    assert "harness.session_tools/v1" in docs
    assert "harness.session_tool_policy_projection/v1" in docs
    assert "Golden Fixtures And Migration Guardrails" in docs
