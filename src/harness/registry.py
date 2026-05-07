from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic import ValidationError

from harness.specs import (
    AgentKind,
    AgentProfileSpec,
    AgentSpec,
    MemoryScope,
    ModelProfile,
    ToolPermission,
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
    agent_profiles: dict[str, AgentProfileSpec] = Field(default_factory=dict)
    workbenches: dict[str, WorkbenchSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> SpecRegistry:
        _validate_mapping_ids("model_profile", self.model_profiles)
        _validate_mapping_ids("memory_scope", self.memory_scopes)
        _validate_mapping_ids("agent", self.agents)
        _validate_mapping_ids("agent_profile", self.agent_profiles)
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
            if agent.parent is not None:
                parent = self.agents[agent.parent]
                if parent.kind != AgentKind.GROUP:
                    raise ValueError(f"Agent {agent_id} parent is not a group: {agent.parent}")
                _validate_child_policy_not_broader(
                    agent_id=agent_id,
                    parent_id=agent.parent,
                    parent_policy=self.tool_policies[parent.tool_policy],
                    child_policy=self.tool_policies[agent.tool_policy],
                )
        for workbench_id, workbench in self.workbenches.items():
            if workbench.default_model_profile not in self.model_profiles:
                raise ValueError(
                    f"Workbench {workbench_id} references missing default_model_profile: "
                    f"{workbench.default_model_profile}"
                )
            for agent_id in workbench.allowed_agents:
                if agent_id not in self.agents:
                    raise ValueError(f"Workbench {workbench_id} references missing allowed agent: {agent_id}")
        for profile_id, profile in self.agent_profiles.items():
            if profile.agent_id not in self.agents:
                raise ValueError(f"Agent profile {profile_id} references missing agent: {profile.agent_id}")
            _validate_profile_forbidden_actions(profile_id=profile_id, profile=profile)
        _validate_agent_parent_cycles(self.agents)
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

    def get_agent_parent_chain(self, agent_id: str) -> list[AgentSpec]:
        agent = self.get_agent(agent_id)
        chain: list[AgentSpec] = []
        while agent.parent is not None:
            parent = self.get_agent(agent.parent)
            chain.insert(0, parent)
            agent = parent
        return chain

    def resolve_agent_effective_spec(self, agent_id: str) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        parent_chain = self.get_agent_parent_chain(agent_id)
        lineage = [*parent_chain, agent]
        tags = _merge_unique(item for spec in lineage for item in spec.tags)
        outputs = _merge_unique(item for spec in lineage for item in spec.outputs)
        return {
            "id": agent.id,
            "kind": agent.kind.value,
            "parent_chain": [parent.id for parent in parent_chain],
            "model_profile": agent.model_profile,
            "tool_policy": agent.tool_policy,
            "memory_scope": agent.memory_scope,
            "tags": tags,
            "outputs": outputs,
            "resolved_from": {
                "agent": agent.id,
                "parents": [parent.id for parent in parent_chain],
            },
        }

    def list_agent_profiles(self, agent_id: str | None = None) -> list[AgentProfileSpec]:
        profiles = sorted(self.agent_profiles.values(), key=lambda profile: profile.id)
        if agent_id is None:
            return profiles
        self.get_agent(agent_id)
        return [profile for profile in profiles if profile.agent_id == agent_id]


def _validate_mapping_ids(kind: str, mapping: dict[str, object]) -> None:
    for key, value in mapping.items():
        if not key.strip():
            raise ValueError(f"{kind} mapping key must be non-empty.")
        value_id = getattr(value, "id", None)
        if key != value_id:
            raise ValueError(f"{kind} mapping key must match contained id: {key} != {value_id}")


def _validate_agent_parent_cycles(agents: dict[str, AgentSpec]) -> None:
    for agent_id in agents:
        seen: set[str] = set()
        current = agents[agent_id]
        while current.parent is not None:
            if current.parent in seen or current.parent == agent_id:
                raise ValueError(f"Agent parent cycle detected: {agent_id}")
            seen.add(current.parent)
            current = agents[current.parent]


def _validate_child_policy_not_broader(
    *,
    agent_id: str,
    parent_id: str,
    parent_policy: ToolPolicy,
    child_policy: ToolPolicy,
) -> None:
    for field_name in ("network", "active_repo_write", "hosted_boundary"):
        parent_value = getattr(parent_policy, field_name)
        child_value = getattr(child_policy, field_name)
        if _permission_rank(child_value) < _permission_rank(parent_value):
            raise ValueError(
                f"Agent {agent_id} broadens parent {parent_id} {field_name}: "
                f"{parent_value.value} -> {child_value.value}"
            )
    for tool_id, parent_value in parent_policy.tools.items():
        child_value = child_policy.tools.get(tool_id, parent_value)
        if _permission_rank(child_value) < _permission_rank(parent_value):
            raise ValueError(
                f"Agent {agent_id} broadens parent {parent_id} tool {tool_id}: "
                f"{parent_value.value} -> {child_value.value}"
            )


def _validate_profile_forbidden_actions(*, profile_id: str, profile: AgentProfileSpec) -> None:
    for action in profile.forbidden_actions:
        if not action.strip():
            raise ValueError(f"Agent profile {profile_id} forbidden action must be non-empty.")


def _permission_rank(permission: ToolPermission) -> int:
    return {
        ToolPermission.ALLOWED: 0,
        ToolPermission.APPROVAL_REQUIRED: 1,
        ToolPermission.FORBIDDEN: 2,
    }[permission]


def _merge_unique(values) -> list[str]:
    merged: list[str] = []
    for value in values:
        if value not in merged:
            merged.append(value)
    return merged


def builtin_spec_registry() -> SpecRegistry:
    return load_packaged_spec_registry()


def load_packaged_spec_registry(root: Path = BUILTIN_SPECS_DIR) -> SpecRegistry:
    spec_root = root.resolve()
    registry_data: dict[str, dict[str, Any]] = {
        "model_profiles": {},
        "tool_policies": {},
        "memory_scopes": {},
        "agents": {},
        "agent_profiles": {},
        "workbenches": {},
    }
    for section, (filename, _model) in BUILTIN_MAPPING_FILES.items():
        registry_data[section] = _load_mapping_file(spec_root / filename, section=section, root=spec_root)
    registry_data["agents"] = _load_spec_tree(spec_root / "agents", section="agents")
    registry_data["agent_profiles"] = _load_profile_tree(spec_root / "agents")
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
        if "profiles" in path.parts:
            continue
        _ensure_packaged_path(path, root)
        data = _load_yaml_mapping(path, root=root)
        if path.name == "group.yaml" and "id" not in data:
            continue
        for spec_id, spec_data in _collect_id_mapping([data], section=section, source=path).items():
            if spec_id in collected:
                raise ValueError(f"Duplicate packaged built-in {section} id: {spec_id}")
            collected[spec_id] = spec_data
    return collected


def _load_profile_tree(root: Path) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Packaged built-in spec directory is missing: {root}")
    collected: dict[str, Any] = {}
    for path in sorted(root.rglob("profiles/*.yaml")):
        _ensure_packaged_path(path, root)
        data = _load_yaml_mapping(path, root=root)
        for profile_id, profile_data in _collect_id_mapping([data], section="agent_profiles", source=path).items():
            if profile_id in collected:
                raise ValueError(f"Duplicate packaged built-in agent_profiles id: {profile_id}")
            collected[profile_id] = profile_data
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
