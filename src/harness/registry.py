from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic import ValidationError

from harness.specs import (
    AgentSpec,
    MemoryScope,
    ModelProfile,
    ToolPolicy,
    WorkbenchSpec,
)


BUILTIN_SPECS_DIR = Path(__file__).with_name("builtin_specs")
BUILTIN_MAPPING_FILES = {
    "model_profiles": ("model_profiles.yaml", ModelProfile),
    "tool_policies": ("tool_policies.yaml", ToolPolicy),
    "memory_scopes": ("memory_scopes.yaml", MemoryScope),
}


class SpecRegistry(BaseModel):
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    tool_policies: dict[str, ToolPolicy] = Field(default_factory=dict)
    memory_scopes: dict[str, MemoryScope] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    workbenches: dict[str, WorkbenchSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> SpecRegistry:
        _validate_mapping_ids("model_profile", self.model_profiles)
        _validate_mapping_ids("memory_scope", self.memory_scopes)
        _validate_mapping_ids("agent", self.agents)
        _validate_mapping_ids("workbench", self.workbenches)
        for policy_id in self.tool_policies:
            if not policy_id.strip():
                raise ValueError("Tool policy id must be non-empty.")
        for agent_id, agent in self.agents.items():
            if agent.model_profile not in self.model_profiles:
                raise ValueError(f"Agent {agent_id} references missing model_profile: {agent.model_profile}")
            if agent.tool_policy not in self.tool_policies:
                raise ValueError(f"Agent {agent_id} references missing tool_policy: {agent.tool_policy}")
            if agent.memory_scope not in self.memory_scopes:
                raise ValueError(f"Agent {agent_id} references missing memory_scope: {agent.memory_scope}")
            if agent.parent is not None and agent.parent not in self.agents:
                raise ValueError(f"Agent {agent_id} references missing parent: {agent.parent}")
        for workbench_id, workbench in self.workbenches.items():
            if workbench.default_model_profile not in self.model_profiles:
                raise ValueError(
                    f"Workbench {workbench_id} references missing default_model_profile: "
                    f"{workbench.default_model_profile}"
                )
            for agent_id in workbench.allowed_agents:
                if agent_id not in self.agents:
                    raise ValueError(f"Workbench {workbench_id} references missing allowed agent: {agent_id}")
        return self

    def get_agent(self, agent_id: str) -> AgentSpec:
        try:
            return self.agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Agent not found: {agent_id}") from exc

    def get_workbench(self, workbench_id: str) -> WorkbenchSpec:
        try:
            return self.workbenches[workbench_id]
        except KeyError as exc:
            raise KeyError(f"Workbench not found: {workbench_id}") from exc


def _validate_mapping_ids(kind: str, mapping: dict[str, object]) -> None:
    for key, value in mapping.items():
        if not key.strip():
            raise ValueError(f"{kind} mapping key must be non-empty.")
        value_id = getattr(value, "id", None)
        if key != value_id:
            raise ValueError(f"{kind} mapping key must match contained id: {key} != {value_id}")


def builtin_spec_registry() -> SpecRegistry:
    return load_packaged_spec_registry()


def load_packaged_spec_registry(root: Path = BUILTIN_SPECS_DIR) -> SpecRegistry:
    spec_root = root.resolve()
    registry_data: dict[str, dict[str, Any]] = {
        "model_profiles": {},
        "tool_policies": {},
        "memory_scopes": {},
        "agents": {},
        "workbenches": {},
    }
    for section, (filename, _model) in BUILTIN_MAPPING_FILES.items():
        registry_data[section] = _load_mapping_file(spec_root / filename, section=section, root=spec_root)
    registry_data["agents"] = _load_spec_tree(spec_root / "agents", section="agents")
    registry_data["workbenches"] = _load_spec_tree(spec_root / "workbenches", section="workbenches")
    try:
        return SpecRegistry.model_validate(registry_data)
    except ValidationError as exc:
        raise ValueError(f"Packaged built-in specs are invalid: {exc}") from exc


def _load_mapping_file(path: Path, *, section: str, root: Path) -> dict[str, Any]:
    data = _load_yaml_mapping(path, root=root)
    values = data.get(section, data)
    if not isinstance(values, dict):
        raise ValueError(f"Packaged built-in spec section must be a mapping: {section}")
    if section == "tool_policies":
        return {key: _validate_mapping_value(key, value, section=section, source=path) for key, value in values.items()}
    return _collect_id_mapping(values.values(), section=section, source=path)


def _load_spec_tree(root: Path, *, section: str) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Packaged built-in spec directory is missing: {root}")
    collected: dict[str, Any] = {}
    for path in sorted(root.rglob("*.yaml")):
        _ensure_packaged_path(path, root)
        data = _load_yaml_mapping(path, root=root)
        if path.name == "group.yaml" and "id" not in data:
            continue
        for spec_id, spec_data in _collect_id_mapping([data], section=section, source=path).items():
            if spec_id in collected:
                raise ValueError(f"Duplicate packaged built-in {section} id: {spec_id}")
            collected[spec_id] = spec_data
    return collected


def _collect_id_mapping(items, *, section: str, source: Path) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Packaged built-in spec entry must be a mapping in {source}")
        spec_id = item.get("id")
        if not isinstance(spec_id, str) or not spec_id.strip():
            raise ValueError(f"Packaged built-in {section} entry missing id in {source}")
        if spec_id in collected:
            raise ValueError(f"Duplicate packaged built-in {section} id: {spec_id}")
        collected[spec_id] = item
    return collected


def _validate_mapping_value(key: str, value: Any, *, section: str, source: Path) -> Any:
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"Packaged built-in {section} mapping key must be non-empty in {source}")
    if not isinstance(value, dict):
        raise ValueError(f"Packaged built-in {section} entry must be a mapping in {source}")
    return value


def _load_yaml_mapping(path: Path, *, root: Path) -> dict[str, Any]:
    _ensure_packaged_path(path, root)
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Packaged built-in spec could not be loaded: {path}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Packaged built-in spec must be a mapping: {path}")
    return data


def _ensure_packaged_path(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Packaged built-in spec path is outside the built-in specs directory: {path}") from exc
    if resolved_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"Packaged built-in spec has unsupported extension: {path}")
