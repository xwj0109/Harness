from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from harness.registry import SpecRegistry


class SpecBundleError(ValueError):
    pass


FORBIDDEN_SPEC_PATH_PARTS = {".harness", ".git", "secrets"}
FORBIDDEN_SPEC_SUFFIXES = {".pem", ".key", ".sqlite"}
SUPPORTED_SPEC_SUFFIXES = {".json", ".yaml", ".yml"}
SPEC_BUNDLE_SCHEMA_VERSION = "harness.spec_bundle/v1"
SPEC_VALIDATION_SCHEMA_VERSION = "harness.spec_validation/v1"
SPEC_EXPORT_SCHEMA_VERSION = "harness.spec_export/v1"
SPEC_DIFF_SCHEMA_VERSION = "harness.spec_diff/v1"
SPEC_EFFECTIVE_PREVIEW_SCHEMA_VERSION = "harness.spec_effective_preview/v1"


def load_spec_registry(path: Path) -> SpecRegistry:
    spec_path = resolve_spec_bundle_path(path)
    try:
        raw = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecBundleError(f"Spec bundle could not be read: {exc}") from exc
    data = _registry_data_from_bundle(_parse_spec_bundle(raw, spec_path.suffix.lower()))
    try:
        return SpecRegistry.model_validate(data)
    except ValidationError as exc:
        raise SpecBundleError(str(exc)) from exc


def validate_spec_bundle(path: Path) -> dict:
    spec_path = path.expanduser().resolve()
    try:
        registry = load_spec_registry(spec_path)
    except SpecBundleError as exc:
        return {
            "schema_version": SPEC_VALIDATION_SCHEMA_VERSION,
            "ok": False,
            "path": str(spec_path),
            "errors": [str(exc)],
        }
    return {
        "schema_version": SPEC_VALIDATION_SCHEMA_VERSION,
        "ok": True,
        "path": str(spec_path),
        "errors": [],
        "registry": {
            "model_profiles": _dump_spec_mapping(registry.model_profiles),
            "tool_policies": _dump_spec_mapping(registry.tool_policies),
            "memory_scopes": _dump_spec_mapping(registry.memory_scopes),
            "agents": _dump_spec_mapping(registry.agents),
            "workbenches": _dump_spec_mapping(registry.workbenches),
        },
    }


def export_builtin_spec_registry(registry: SpecRegistry) -> dict:
    return export_spec_registry(registry, source_kind="builtin", source_path=None)


def export_custom_spec_registry(path: Path) -> dict:
    spec_path = resolve_spec_bundle_path(path)
    registry = load_spec_registry(spec_path)
    return export_spec_registry(registry, source_kind="custom", source_path=spec_path)


def export_spec_registry(registry: SpecRegistry, *, source_kind: str, source_path: Path | None) -> dict:
    registry_sections = {
        "agents": _dump_spec_mapping(registry.agents),
        "memory_scopes": _dump_spec_mapping(registry.memory_scopes),
        "model_profiles": _dump_spec_mapping(registry.model_profiles),
        "tool_policies": _dump_spec_mapping(registry.tool_policies),
        "workbenches": _dump_spec_mapping(registry.workbenches),
    }
    return {
        "schema_version": SPEC_EXPORT_SCHEMA_VERSION,
        "source": {
            "kind": source_kind,
            "path": str(source_path) if source_path is not None else None,
        },
        "registry": {section: registry_sections[section] for section in sorted(registry_sections)},
    }


def diff_spec_registries(base_registry: SpecRegistry, compare_registry: SpecRegistry, *, compare_path: Path) -> dict:
    base_export = export_builtin_spec_registry(base_registry)
    compare_export = export_spec_registry(compare_registry, source_kind="custom", source_path=compare_path)
    return {
        "schema_version": SPEC_DIFF_SCHEMA_VERSION,
        "source": {
            "base": base_export["source"],
            "compare": compare_export["source"],
        },
        "diff": _diff_registry_payloads(base_export["registry"], compare_export["registry"]),
    }


def diff_builtin_to_custom_spec_registry(base_registry: SpecRegistry, path: Path) -> dict:
    spec_path = resolve_spec_bundle_path(path)
    compare_registry = load_spec_registry(spec_path)
    return diff_spec_registries(base_registry, compare_registry, compare_path=spec_path)


def preview_agent_effective_policy(registry: SpecRegistry, agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    return {
        "agent": _dump_model(agent),
        "parent": agent.parent,
        "model_profile": _dump_model(registry.model_profiles[agent.model_profile]),
        "tool_policy": _dump_model(registry.tool_policies[agent.tool_policy]),
        "memory_scope": _dump_model(registry.memory_scopes[agent.memory_scope]),
    }


def preview_workbench_effective_policy(registry: SpecRegistry, workbench_id: str) -> dict:
    workbench = registry.get_workbench(workbench_id)
    allowed_agents = {}
    for agent_id in sorted(workbench.allowed_agents):
        agent = registry.agents[agent_id]
        allowed_agents[agent_id] = {
            "agent": _dump_model(agent),
            "model_profile": _dump_model(registry.model_profiles[agent.model_profile]),
            "tool_policy": _dump_model(registry.tool_policies[agent.tool_policy]),
            "memory_scope": _dump_model(registry.memory_scopes[agent.memory_scope]),
        }
    return {
        "workbench": _dump_model(workbench),
        "default_model_profile": _dump_model(registry.model_profiles[workbench.default_model_profile]),
        "allowed_agents": allowed_agents,
        "forbidden_actions": sorted(workbench.forbidden_actions),
        "local_model_profiles": _dump_spec_mapping(workbench.model_profiles),
        "local_tool_policies": _dump_spec_mapping(workbench.tool_policies),
        "local_memory_scopes": _dump_spec_mapping(workbench.memory_scopes),
        "approval_policy": _sort_json(workbench.approval_policy),
    }


def effective_policy_preview(
    registry: SpecRegistry,
    *,
    target_kind: str,
    target_id: str,
    source_kind: str,
    source_path: Path | None,
) -> dict:
    if target_kind == "agent":
        preview = preview_agent_effective_policy(registry, target_id)
    elif target_kind == "workbench":
        preview = preview_workbench_effective_policy(registry, target_id)
    else:
        raise ValueError(f"Unsupported effective preview target kind: {target_kind}")
    return {
        "schema_version": SPEC_EFFECTIVE_PREVIEW_SCHEMA_VERSION,
        "source": {
            "kind": source_kind,
            "path": str(source_path) if source_path is not None else None,
        },
        "target": {
            "kind": target_kind,
            "id": target_id,
        },
        "preview": preview,
    }


def resolve_spec_bundle_path(path: Path) -> Path:
    spec_path = path.expanduser().resolve()
    _validate_spec_bundle_path(spec_path)
    return spec_path


def _validate_spec_bundle_path(path: Path) -> None:
    suffix = path.suffix.lower()
    if any(part in FORBIDDEN_SPEC_PATH_PARTS for part in path.parts):
        raise SpecBundleError("Spec bundle path is forbidden by harness safety policy.")
    name = path.name
    if name.startswith(".env"):
        raise SpecBundleError("Spec bundle path is forbidden by harness safety policy.")
    if suffix in FORBIDDEN_SPEC_SUFFIXES:
        raise SpecBundleError("Spec bundle path is forbidden by harness safety policy.")
    if suffix not in SUPPORTED_SPEC_SUFFIXES:
        raise SpecBundleError(f"Unsupported spec bundle extension: {suffix or '<none>'}")
    if not path.exists():
        raise SpecBundleError(f"Spec bundle does not exist: {path}")
    if not path.is_file():
        raise SpecBundleError(f"Spec bundle is not a file: {path}")


def _parse_spec_bundle(raw: str, suffix: str) -> Any:
    try:
        if suffix == ".json":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SpecBundleError(f"Spec bundle could not be parsed: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SpecBundleError("Spec bundle must be a mapping object.")
    return data


def _registry_data_from_bundle(data: dict) -> dict:
    schema_version = data.get("schema_version")
    if schema_version is None:
        raise SpecBundleError("Spec bundle missing schema_version.")
    if schema_version != SPEC_BUNDLE_SCHEMA_VERSION:
        raise SpecBundleError(f"Unsupported spec bundle schema_version: {schema_version}")
    return {key: value for key, value in data.items() if key != "schema_version"}


def _dump_spec_mapping(mapping: dict) -> dict:
    return {key: _dump_model(mapping[key]) for key in sorted(mapping)}


def _dump_model(model) -> dict:
    return _sort_json(model.model_dump(mode="json"))


def _sort_json(value):
    if isinstance(value, dict):
        return {key: _sort_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_json(item) for item in value]
    return value


def _diff_registry_payloads(base_registry: dict, compare_registry: dict) -> dict:
    diff = {}
    for section in sorted(set(base_registry) | set(compare_registry)):
        base_section = base_registry.get(section, {})
        compare_section = compare_registry.get(section, {})
        base_ids = set(base_section)
        compare_ids = set(compare_section)
        shared_ids = base_ids & compare_ids
        diff[section] = {
            "added": sorted(compare_ids - base_ids),
            "removed": sorted(base_ids - compare_ids),
            "changed": sorted(spec_id for spec_id in shared_ids if base_section[spec_id] != compare_section[spec_id]),
            "unchanged": sorted(spec_id for spec_id in shared_ids if base_section[spec_id] == compare_section[spec_id]),
        }
    return diff
