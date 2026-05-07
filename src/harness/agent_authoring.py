from __future__ import annotations

from pathlib import Path
from typing import Any

import hashlib
import json
import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from harness.registry import SpecRegistry, builtin_spec_registry
from harness.spec_loader import preview_agent_effective_policy
from harness.specs import AgentProfileSpec, AgentSpec


AGENT_BUNDLE_SCHEMA_VERSION = "harness.agent_bundle/v1"
AGENT_SCAFFOLD_SCHEMA_VERSION = "harness.agent_scaffold/v1"
AGENT_BUNDLE_VALIDATION_SCHEMA_VERSION = "harness.agent_bundle_validation/v1"
AGENT_BUNDLE_PREVIEW_SCHEMA_VERSION = "harness.agent_bundle_preview/v1"

FORBIDDEN_AGENT_PATH_PARTS = {".harness", ".git", "secrets"}
FORBIDDEN_AGENT_SUFFIXES = {".pem", ".key", ".sqlite"}
SUPPORTED_PROFILE_SUFFIXES = {".yaml", ".yml"}


class AgentBundleError(ValueError):
    pass


class AgentBundle(BaseModel):
    schema_version: str
    workbench_id: str
    agent: AgentSpec

    @model_validator(mode="after")
    def validate_schema_version(self) -> AgentBundle:
        if self.schema_version != AGENT_BUNDLE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported agent bundle schema_version: {self.schema_version}")
        return self


class LoadedAgentBundle(BaseModel):
    source_path: Path
    bundle: AgentBundle
    profiles: list[AgentProfileSpec] = Field(default_factory=list)


def scaffold_agent_bundle(
    *,
    agent_id: str,
    workbench_id: str,
    kind: str,
    parent: str | None,
    model_profile: str,
    tool_policy: str,
    memory_scope: str,
    output_path: Path,
    role: str = "Custom declarative agent.",
) -> dict:
    destination = _resolve_output_path(output_path)
    if destination.exists() and not destination.is_dir():
        raise AgentBundleError(f"Agent bundle destination is not a directory: {destination}")
    if destination.exists() and any(destination.iterdir()):
        raise AgentBundleError(f"Agent bundle destination is not empty: {destination}")
    registry = builtin_spec_registry()
    _validate_scaffold_references(
        registry=registry,
        agent_id=agent_id,
        workbench_id=workbench_id,
        parent=parent,
        model_profile=model_profile,
        tool_policy=tool_policy,
        memory_scope=memory_scope,
    )
    agent_data = {
        "id": agent_id,
        "kind": kind,
        "role": role,
        "model_profile": model_profile,
        "tool_policy": tool_policy,
        "memory_scope": memory_scope,
        "parent": parent,
        "outputs": [],
        "tags": [],
    }
    profile_data = {
        "id": f"{agent_id}.default",
        "agent_id": agent_id,
        "description": f"Default profile for {agent_id}.",
        "knowledge_domains": [],
        "preferred_outputs": [],
        "review_responsibilities": [],
        "forbidden_actions": [],
        "tags": [],
        "metadata": {},
    }
    try:
        bundle = AgentBundle(
            schema_version=AGENT_BUNDLE_SCHEMA_VERSION,
            workbench_id=workbench_id,
            agent=AgentSpec.model_validate(agent_data),
        )
        profiles = [AgentProfileSpec.model_validate(profile_data)]
    except ValidationError as exc:
        raise AgentBundleError(str(exc)) from exc
    merged = _build_merged_registry(registry, bundle, profiles)
    destination.mkdir(parents=True, exist_ok=True)
    profiles_dir = destination / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    _write_yaml(
        destination / "agent.yaml",
        {
            "schema_version": AGENT_BUNDLE_SCHEMA_VERSION,
            "workbench_id": workbench_id,
            "agent": agent_data,
        },
    )
    _write_yaml(profiles_dir / "default.yaml", profile_data)
    return {
        "schema_version": AGENT_SCAFFOLD_SCHEMA_VERSION,
        "ok": True,
        "source_path": str(destination),
        "agent_id": agent_id,
        "workbench_id": workbench_id,
        "profiles": [profile_data],
        "errors": [],
        "warnings": [],
        "registry_validated": merged.get_agent(agent_id).id == agent_id,
    }


def validate_agent_bundle(path: Path) -> dict:
    source_path = path.expanduser().resolve()
    try:
        loaded = load_agent_bundle(path)
        registry = merge_agent_bundle_with_builtins(loaded)
    except AgentBundleError as exc:
        return _validation_error(source_path, str(exc))
    return {
        "schema_version": AGENT_BUNDLE_VALIDATION_SCHEMA_VERSION,
        "ok": True,
        "source_path": str(loaded.source_path),
        "agent_id": loaded.bundle.agent.id,
        "workbench_id": loaded.bundle.workbench_id,
        "profiles": [_dump_model(profile) for profile in loaded.profiles],
        "errors": [],
        "warnings": [],
        "registry_validated": registry.get_agent(loaded.bundle.agent.id).id == loaded.bundle.agent.id,
    }


def preview_agent_bundle(path: Path) -> dict:
    source_path = path.expanduser().resolve()
    try:
        loaded = load_agent_bundle(path)
        registry = merge_agent_bundle_with_builtins(loaded)
        preview = preview_agent_effective_policy(registry, loaded.bundle.agent.id)
        workbench = registry.get_workbench(loaded.bundle.workbench_id)
    except (AgentBundleError, KeyError) as exc:
        return {
            "schema_version": AGENT_BUNDLE_PREVIEW_SCHEMA_VERSION,
            "ok": False,
            "source_path": str(source_path),
            "agent": None,
            "profiles": [],
            "parent_chain": [],
            "effective_agent": None,
            "workbench": None,
            "errors": [str(exc).strip("'")],
            "warnings": [],
        }
    return {
        "schema_version": AGENT_BUNDLE_PREVIEW_SCHEMA_VERSION,
        "ok": True,
        "source_path": str(loaded.source_path),
        "agent": preview["agent"],
        "profiles": preview["profiles"],
        "parent_chain": preview["parent_chain"],
        "effective_agent": preview["effective_agent"],
        "workbench": _dump_model(workbench),
        "errors": [],
        "warnings": [],
    }


def load_agent_bundle(path: Path) -> LoadedAgentBundle:
    bundle_path = resolve_agent_bundle_path(path)
    agent_path = bundle_path / "agent.yaml"
    if not agent_path.exists():
        raise AgentBundleError(f"Agent bundle missing agent.yaml: {bundle_path}")
    agent_data = _load_yaml_mapping(agent_path)
    if "agent" in agent_data and isinstance(agent_data["agent"], dict):
        agent_data["agent"] = _normalize_agent_data(agent_data["agent"])
    try:
        bundle = AgentBundle.model_validate(agent_data)
    except ValidationError as exc:
        raise AgentBundleError(str(exc)) from exc
    profiles = _load_profiles(bundle_path, bundle.agent.id)
    return LoadedAgentBundle(source_path=bundle_path, bundle=bundle, profiles=profiles)


def merge_agent_bundle_with_builtins(loaded: LoadedAgentBundle) -> SpecRegistry:
    builtin = builtin_spec_registry()
    return _build_merged_registry(builtin, loaded.bundle, loaded.profiles)


def agent_bundle_content_sha256(loaded: LoadedAgentBundle) -> str:
    payload = {
        "agent": _dump_model(loaded.bundle.agent),
        "profiles": [_dump_model(profile) for profile in sorted(loaded.profiles, key=lambda item: item.id)],
        "schema_version": loaded.bundle.schema_version,
        "workbench_id": loaded.bundle.workbench_id,
    }
    encoded = json.dumps(_sort_json(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_agent_bundle_path(path: Path) -> Path:
    expanded = path.expanduser()
    _reject_symlink_components(expanded)
    bundle_path = expanded.resolve()
    _validate_agent_path(bundle_path)
    if not bundle_path.exists():
        raise AgentBundleError(f"Agent bundle does not exist: {bundle_path}")
    if not bundle_path.is_dir():
        raise AgentBundleError(f"Agent bundle path is not a directory: {bundle_path}")
    return bundle_path


def _build_merged_registry(
    builtin: SpecRegistry,
    bundle: AgentBundle,
    profiles: list[AgentProfileSpec],
) -> SpecRegistry:
    agent_id = bundle.agent.id
    if agent_id in builtin.agents:
        raise AgentBundleError(f"Custom agent id shadows built-in agent: {agent_id}")
    if bundle.workbench_id not in builtin.workbenches:
        raise AgentBundleError(f"Workbench not found: {bundle.workbench_id}")
    for profile in profiles:
        if profile.agent_id != agent_id:
            raise AgentBundleError(f"Agent profile {profile.id} must reference bundle agent: {agent_id}")
        if profile.id in builtin.agent_profiles:
            raise AgentBundleError(f"Custom agent profile id shadows built-in profile: {profile.id}")
    profile_ids = [profile.id for profile in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise AgentBundleError("Duplicate custom agent profile id.")
    try:
        return SpecRegistry(
            model_profiles=dict(builtin.model_profiles),
            tool_policies=dict(builtin.tool_policies),
            memory_scopes=dict(builtin.memory_scopes),
            agents={**builtin.agents, agent_id: bundle.agent},
            agent_profiles={**builtin.agent_profiles, **{profile.id: profile for profile in profiles}},
            workbenches=dict(builtin.workbenches),
        )
    except ValidationError as exc:
        raise AgentBundleError(str(exc)) from exc


def _validate_scaffold_references(
    *,
    registry: SpecRegistry,
    agent_id: str,
    workbench_id: str,
    parent: str | None,
    model_profile: str,
    tool_policy: str,
    memory_scope: str,
) -> None:
    if agent_id in registry.agents:
        raise AgentBundleError(f"Custom agent id shadows built-in agent: {agent_id}")
    if workbench_id not in registry.workbenches:
        raise AgentBundleError(f"Workbench not found: {workbench_id}")
    if parent is not None and parent not in registry.agents:
        raise AgentBundleError(f"Agent {agent_id} references missing parent: {parent}")
    if model_profile not in registry.model_profiles:
        raise AgentBundleError(f"Agent {agent_id} references missing model_profile: {model_profile}")
    if tool_policy not in registry.tool_policies:
        raise AgentBundleError(f"Agent {agent_id} references missing tool_policy: {tool_policy}")
    if memory_scope not in registry.memory_scopes:
        raise AgentBundleError(f"Agent {agent_id} references missing memory_scope: {memory_scope}")


def _load_profiles(bundle_path: Path, agent_id: str) -> list[AgentProfileSpec]:
    profiles_dir = bundle_path / "profiles"
    if not profiles_dir.exists():
        return []
    if not profiles_dir.is_dir():
        raise AgentBundleError(f"Agent bundle profiles path is not a directory: {profiles_dir}")
    profiles: list[AgentProfileSpec] = []
    for path in sorted(profiles_dir.iterdir()):
        _validate_agent_path(path)
        if path.is_dir():
            raise AgentBundleError(f"Agent profile entry is not a file: {path}")
        if path.suffix.lower() not in SUPPORTED_PROFILE_SUFFIXES:
            raise AgentBundleError(f"Unsupported agent profile extension: {path.suffix or '<none>'}")
        try:
            profile = AgentProfileSpec.model_validate(_load_yaml_mapping(path))
        except ValidationError as exc:
            raise AgentBundleError(str(exc)) from exc
        if profile.agent_id != agent_id:
            raise AgentBundleError(f"Agent profile {profile.id} must reference bundle agent: {agent_id}")
        profiles.append(profile)
    return profiles


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    _validate_agent_path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgentBundleError(f"Agent bundle YAML could not be parsed: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise AgentBundleError(f"Agent bundle YAML must be a mapping: {path}")
    return data


def _normalize_agent_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    if "role" not in normalized and "description" in normalized:
        normalized["role"] = normalized.pop("description")
    for legacy, current in (
        ("model_profile_id", "model_profile"),
        ("tool_policy_id", "tool_policy"),
        ("memory_scope_id", "memory_scope"),
    ):
        if current not in normalized and legacy in normalized:
            normalized[current] = normalized.pop(legacy)
    return normalized


def _resolve_output_path(path: Path) -> Path:
    expanded = path.expanduser()
    _reject_symlink_components(expanded)
    output_path = expanded.resolve()
    _validate_agent_path(output_path)
    return output_path


def _validate_agent_path(path: Path) -> None:
    _reject_symlink_components(path)
    if any(part in FORBIDDEN_AGENT_PATH_PARTS for part in path.parts):
        raise AgentBundleError("Agent bundle path is forbidden by harness safety policy.")
    if path.name.startswith(".env"):
        raise AgentBundleError("Agent bundle path is forbidden by harness safety policy.")
    if path.suffix.lower() in FORBIDDEN_AGENT_SUFFIXES:
        raise AgentBundleError("Agent bundle path is forbidden by harness safety policy.")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts
    start = 1 if path.is_absolute() else 0
    for part in parts[start:]:
        current = current / part
        if current.is_symlink():
            raise AgentBundleError("Agent bundle path cannot include symlinks.")


def _validation_error(source_path: Path, error: str) -> dict:
    return {
        "schema_version": AGENT_BUNDLE_VALIDATION_SCHEMA_VERSION,
        "ok": False,
        "source_path": str(source_path),
        "agent_id": None,
        "workbench_id": None,
        "profiles": [],
        "errors": [error],
        "warnings": [],
    }


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _dump_model(model) -> dict:
    return _sort_json(model.model_dump(mode="json"))


def _sort_json(value):
    if isinstance(value, dict):
        return {key: _sort_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_json(item) for item in value]
    return value
