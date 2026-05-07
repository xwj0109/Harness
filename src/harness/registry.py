from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from harness.specs import (
    AgentKind,
    AgentSpec,
    MemoryScope,
    ModelProfile,
    ModelProfileKind,
    ToolPermission,
    ToolPolicy,
    WorkbenchSpec,
)


class SpecRegistry(BaseModel):
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    tool_policies: dict[str, ToolPolicy] = Field(default_factory=dict)
    memory_scopes: dict[str, MemoryScope] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    workbenches: dict[str, WorkbenchSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> SpecRegistry:
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


def builtin_spec_registry() -> SpecRegistry:
    model_profiles = {
        "local_reasoning": ModelProfile(
            id="local_reasoning",
            kind=ModelProfileKind.LOCAL,
            backend="local_openai_compatible",
            description="Local model profile for private reasoning tasks.",
            default=True,
        ),
        "codex_supervised": ModelProfile(
            id="codex_supervised",
            kind=ModelProfileKind.EXTERNAL_AGENT,
            backend="codex_cli",
            description="Supervised Codex CLI profile for approved isolated coding work.",
            constraints=["supervised_external_agent"],
        ),
    }
    tool_policies = {
        "read_only": ToolPolicy(
            tools={
                "repo_read": ToolPermission.ALLOWED,
                "artifact_read": ToolPermission.ALLOWED,
            },
            active_repo_write=ToolPermission.FORBIDDEN,
            hosted_boundary=ToolPermission.APPROVAL_REQUIRED,
        ),
        "isolated_code_edit": ToolPolicy(
            tools={
                "artifact_read": ToolPermission.ALLOWED,
                "artifact_write": ToolPermission.ALLOWED,
                "isolated_edit": ToolPermission.ALLOWED,
                "active_repo_apply": ToolPermission.APPROVAL_REQUIRED,
            },
            active_repo_write=ToolPermission.APPROVAL_REQUIRED,
            hosted_boundary=ToolPermission.APPROVAL_REQUIRED,
        ),
        "docker_test": ToolPolicy(
            tools={
                "artifact_read": ToolPermission.ALLOWED,
                "artifact_write": ToolPermission.ALLOWED,
                "docker_tests": ToolPermission.APPROVAL_REQUIRED,
            },
            active_repo_write=ToolPermission.FORBIDDEN,
            hosted_boundary=ToolPermission.FORBIDDEN,
        ),
    }
    memory_scopes = {
        "project": MemoryScope(id="project", description="Project-local memory scope."),
        "quant": MemoryScope(id="quant", description="Quant research memory scope."),
        "personal": MemoryScope(id="personal", description="Personal productivity memory scope."),
    }
    agents = {
        "repo_inspector": AgentSpec(
            id="repo_inspector",
            kind=AgentKind.SPECIALIST,
            role="Inspect local repository structure and evidence without mutation.",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="project",
            outputs=["repo_summary.md"],
            tags=["starter", "coding"],
        ),
        "code_editor": AgentSpec(
            id="code_editor",
            kind=AgentKind.SPECIALIST,
            role="Prepare code edits in controlled isolated workflows.",
            model_profile="codex_supervised",
            tool_policy="isolated_code_edit",
            memory_scope="project",
            outputs=["patch_summary.md"],
            tags=["starter", "coding"],
        ),
        "test_runner": AgentSpec(
            id="test_runner",
            kind=AgentKind.SPECIALIST,
            role="Run approved Docker-sandboxed test workflows and report results.",
            model_profile="local_reasoning",
            tool_policy="docker_test",
            memory_scope="project",
            outputs=["test_report.md"],
            tags=["starter", "coding"],
        ),
        "quant_researcher": AgentSpec(
            id="quant_researcher",
            kind=AgentKind.SPECIALIST,
            role="Draft quant research briefs and data requirement notes.",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="quant",
            outputs=["research_brief.md"],
            tags=["starter", "quant"],
        ),
        "job_researcher": AgentSpec(
            id="job_researcher",
            kind=AgentKind.SPECIALIST,
            role="Draft job research notes without sending applications or messages.",
            model_profile="local_reasoning",
            tool_policy="read_only",
            memory_scope="personal",
            outputs=["job_research.md"],
            tags=["starter", "personal"],
        ),
    }
    workbenches = {
        "coding": WorkbenchSpec(
            id="coding",
            description="Local-first coding workbench.",
            allowed_agents=["repo_inspector", "code_editor", "test_runner"],
            default_model_profile="local_reasoning",
            forbidden_actions=["paid_api_fallback", "hosted_fallback"],
        ),
        "quant": WorkbenchSpec(
            id="quant",
            description="Quant research workbench with no trading or broker actions.",
            allowed_agents=["quant_researcher", "repo_inspector"],
            default_model_profile="local_reasoning",
            forbidden_actions=["live_trading", "broker_action", "capital_allocation"],
        ),
        "personal": WorkbenchSpec(
            id="personal",
            description="Personal productivity workbench for drafts and research only.",
            allowed_agents=["job_researcher"],
            default_model_profile="local_reasoning",
            forbidden_actions=["email_send", "application_submit", "external_message_send"],
        ),
    }
    return SpecRegistry(
        model_profiles=model_profiles,
        tool_policies=tool_policies,
        memory_scopes=memory_scopes,
        agents=agents,
        workbenches=workbenches,
    )
