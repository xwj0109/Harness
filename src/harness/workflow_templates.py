from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.security import sanitize_for_logging


WORKFLOW_TEMPLATE_SCHEMA_VERSION = "harness.workflow_template/v1"


@dataclass(frozen=True)
class WorkflowTaskTemplate:
    title: str
    description: str
    execution_adapter: str
    task_type: str
    agent_id: str | None = None
    workbench_id: str | None = "coding"
    depends_on_indexes: list[int] = field(default_factory=list)
    priority: int = 0
    task_metadata: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        return {
            **dict(self.task_metadata),
            "execution_adapter": self.execution_adapter,
            "task_type": self.task_type,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "execution_adapter": self.execution_adapter,
            "task_type": self.task_type,
            "agent_id": self.agent_id,
            "workbench_id": self.workbench_id,
            "depends_on_indexes": list(self.depends_on_indexes),
            "priority": self.priority,
            "metadata": self.metadata(),
        }


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    interpreted_intent: str
    proposed_action: str
    objective_title: str
    objective_description: str
    tasks: list[WorkflowTaskTemplate]
    required_approvals: list[str] = field(default_factory=list)
    safety_boundary: list[str] = field(default_factory=list)
    equivalent_commands: list[str] = field(default_factory=list)
    confirm_prompt: str = "Type yes or /confirm to continue. Type no to cancel."

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": WORKFLOW_TEMPLATE_SCHEMA_VERSION,
            "id": self.id,
            "interpreted_intent": self.interpreted_intent,
            "proposed_action": self.proposed_action,
            "objective_title": self.objective_title,
            "objective_description": self.objective_description,
            "tasks": [task.to_payload() for task in self.tasks],
            "required_approvals": list(self.required_approvals),
            "safety_boundary": list(self.safety_boundary),
            "equivalent_commands": list(self.equivalent_commands),
            "confirm_prompt": self.confirm_prompt,
        }


def repo_summary_template(prompt: str, project_root: Path) -> WorkflowTemplate:
    goal = _goal(prompt)
    return WorkflowTemplate(
        id="repo_summary",
        interpreted_intent="repo_summary",
        proposed_action="Create one read-only repository summary task and dispatch it through the registered adapter after confirmation.",
        objective_title="Repository summary",
        objective_description=f"Chat-requested repository summary: {goal}",
        tasks=[
            WorkflowTaskTemplate(
                title="Chat read-only summary",
                description=f"Read-only summary requested from chat: {goal}",
                execution_adapter="read_only_summary",
                task_type="read_only_repo_summary",
                agent_id="repo_inspector",
                priority=1000,
            )
        ],
        required_approvals=["hosted_provider_codex"],
        safety_boundary=_codex_read_only_boundary(),
        equivalent_commands=[
            f'harness tasks add --title "Chat read-only summary" --execution-adapter read_only_summary --task-type read_only_repo_summary --project {project_root} --output json',
            f"harness daemon run-once --project {project_root} --output json",
            f"harness daemon execute <lease_id> --project {project_root} --output json",
        ],
    )


def repo_planning_template(prompt: str, project_root: Path) -> WorkflowTemplate:
    goal = _goal(prompt)
    return WorkflowTemplate(
        id="repo_planning",
        interpreted_intent="repo_planning",
        proposed_action="Create one read-only repo-planning task and dispatch it through the registered adapter after confirmation.",
        objective_title="Repository plan",
        objective_description=f"Chat-requested repository plan: {goal}",
        tasks=[
            WorkflowTaskTemplate(
                title="Chat repo planning",
                description=f"Plan the requested repository change without modifying files: {goal}",
                execution_adapter="repo_planning",
                task_type="repo_planning",
                agent_id="repo_inspector",
                priority=1000,
            )
        ],
        required_approvals=["hosted_provider_codex"],
        safety_boundary=_codex_read_only_boundary(),
        equivalent_commands=[
            f'harness tasks add --title "Chat repo planning" --execution-adapter repo_planning --task-type repo_planning --project {project_root} --output json',
            f"harness daemon run-once --project {project_root} --output json",
            f"harness daemon execute <lease_id> --project {project_root} --output json",
        ],
    )


def coding_fix_template(prompt: str, project_root: Path) -> WorkflowTemplate:
    goal = _goal(prompt)
    return WorkflowTemplate(
        id="coding_fix",
        interpreted_intent="codex_isolated_edit",
        proposed_action=(
            "Create a bounded coding workflow: read-only planning, isolated edit, local test evidence, "
            "implementation review, security review, and final synthesis."
        ),
        objective_title=_title_for("Coding fix", goal),
        objective_description=f"Chat-requested coding fix: {goal}",
        tasks=[
            WorkflowTaskTemplate(
                title="Plan the coding fix",
                description=f"Plan the requested fix without modifying files: {goal}",
                execution_adapter="repo_planning",
                task_type="repo_planning",
                agent_id="repo_inspector",
                priority=1000,
                task_metadata={"workflow_stage": "repo_planning", "completion_gate": False},
            ),
            WorkflowTaskTemplate(
                title="Prepare isolated Codex edit",
                description=f"Use Codex in an isolated workspace to prepare the requested fix: {goal}",
                execution_adapter="codex_isolated_edit",
                task_type="codex_code_edit",
                agent_id="code_editor",
                depends_on_indexes=[0],
                priority=999,
                task_metadata={"workflow_stage": "codex_isolated_edit", "completion_gate": False},
            ),
            WorkflowTaskTemplate(
                title="Record sandbox test evidence",
                description=f"Record the approved sandbox test plan and evidence boundary for: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="test_runner",
                depends_on_indexes=[1],
                priority=998,
                task_metadata={
                    "workflow_stage": "test_sandbox",
                    "review_gate": True,
                    "completion_gate": True,
                    "review_target_stage": "codex_isolated_edit",
                },
            ),
            WorkflowTaskTemplate(
                title="Implementation review",
                description=f"Review the isolated edit artifact and test evidence for implementation correctness: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="implementation_reviewer",
                depends_on_indexes=[2],
                priority=997,
                task_metadata={
                    "workflow_stage": "implementation_review",
                    "review_role": "implementation_reviewer",
                    "review_gate": True,
                    "completion_gate": True,
                    "review_target_stage": "codex_isolated_edit",
                },
            ),
            WorkflowTaskTemplate(
                title="Security review",
                description=f"Review the isolated edit artifact for policy, path, secret, and apply-back risks: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="security_reviewer",
                depends_on_indexes=[3],
                priority=996,
                task_metadata={
                    "workflow_stage": "security_review",
                    "review_role": "security_reviewer",
                    "review_gate": True,
                    "completion_gate": True,
                    "blocks_apply_back": True,
                    "review_target_stage": "codex_isolated_edit",
                },
            ),
            WorkflowTaskTemplate(
                title="Final coding workflow report",
                description=f"Synthesize objective, task, run, artifact, review, and policy evidence for: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="coding_orchestrator",
                depends_on_indexes=[0, 1, 2, 3, 4],
                priority=995,
                task_metadata={
                    "workflow_stage": "final_report",
                    "review_gate": True,
                    "completion_gate": True,
                    "requires_evidence_links": "objective,task,run,artifact,policy",
                },
            ),
        ],
        required_approvals=["hosted_provider_codex"],
        safety_boundary=[
            *_codex_read_only_boundary(),
            "The edit task runs only in an isolated workspace.",
            "Local reviewer tasks must produce evidence before the workflow can complete.",
            "The security review gates any later apply-back path.",
            "Apply-back is not automatic and remains denied by default.",
        ],
        equivalent_commands=[
            f'harness objectives add --title "{_title_for("Coding fix", goal)}" --workbench coding --project {project_root} --output json',
            "harness tasks add ... --execution-adapter repo_planning --task-type repo_planning",
            "harness tasks add ... --execution-adapter codex_isolated_edit --task-type codex_code_edit --depends-on <planning_task>",
            "harness tasks add ... --execution-adapter dry_run --task-type phase_1a_test --agent test_runner --depends-on <edit_task>",
            "harness tasks add ... --execution-adapter dry_run --task-type phase_1a_test --agent implementation_reviewer --depends-on <test_task>",
            "harness tasks add ... --execution-adapter dry_run --task-type phase_1a_test --agent security_reviewer --depends-on <implementation_review_task>",
            "harness tasks add ... --execution-adapter dry_run --task-type phase_1a_test --agent coding_orchestrator --depends-on <all_prior_tasks>",
            f"harness daemon run-once --project {project_root} --output json",
            f"harness daemon execute <lease_id> --project {project_root} --output json",
        ],
    )


def research_brief_template(prompt: str, project_root: Path) -> WorkflowTemplate:
    goal = _goal(prompt)
    return WorkflowTemplate(
        id="research_brief",
        interpreted_intent="research_brief",
        proposed_action="Create a bounded research workflow with read-only inspection, a brief, factuality review, and synthesis.",
        objective_title=_title_for("Research brief", goal),
        objective_description=f"Chat-requested research workflow: {goal}",
        tasks=[
            WorkflowTaskTemplate(
                title="Read-only research inspection",
                description=f"Inspect local repository context for the research request without mutation: {goal}",
                execution_adapter="read_only_summary",
                task_type="read_only_repo_summary",
                agent_id="repo_inspector",
                priority=1000,
                task_metadata={"workflow_stage": "read_only_inspection", "completion_gate": False},
            ),
            WorkflowTaskTemplate(
                title="Research brief",
                description=f"Produce local research brief evidence for: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="repo_inspector",
                depends_on_indexes=[0],
                priority=999,
                task_metadata={"workflow_stage": "research_brief", "completion_gate": True},
            ),
            WorkflowTaskTemplate(
                title="Factuality review",
                description=f"Review the research brief for unsupported claims and missing evidence: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="factuality_reviewer",
                depends_on_indexes=[1],
                priority=998,
                task_metadata={
                    "workflow_stage": "factuality_review",
                    "review_role": "factuality_reviewer",
                    "review_gate": True,
                    "completion_gate": True,
                },
            ),
            WorkflowTaskTemplate(
                title="Research synthesis",
                description=f"Synthesize research, factuality review, artifact, and policy evidence for: {goal}",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
                agent_id="coding_orchestrator",
                depends_on_indexes=[0, 1, 2],
                priority=997,
                task_metadata={
                    "workflow_stage": "research_synthesis",
                    "review_gate": True,
                    "completion_gate": True,
                    "requires_evidence_links": "objective,task,run,artifact,policy",
                },
            ),
        ],
        required_approvals=["hosted_provider_codex"],
        safety_boundary=[
            *_codex_read_only_boundary(),
            "Research reviewer tasks produce local evidence only.",
            "Research synthesis cannot grant approvals or broaden policy.",
        ],
        equivalent_commands=[
            f'harness objectives add --title "{_title_for("Research brief", goal)}" --workbench coding --project {project_root} --output json',
            "harness tasks add ... --execution-adapter read_only_summary --task-type read_only_repo_summary",
            "harness tasks add ... --execution-adapter dry_run --task-type phase_1a_test --agent factuality_reviewer",
            f"harness objectives run <objective_id> --autonomy supervised-codex --project {project_root} --output json",
        ],
    )


def template_for_intent(intent: str, prompt: str, project_root: Path) -> WorkflowTemplate:
    if intent == "repo_summary":
        return repo_summary_template(prompt, project_root)
    if intent == "repo_planning":
        return repo_planning_template(prompt, project_root)
    if intent == "coding_fix":
        return coding_fix_template(prompt, project_root)
    if intent == "research_brief":
        return research_brief_template(prompt, project_root)
    raise KeyError(f"Unknown workflow template intent: {intent}")


def _goal(prompt: str) -> str:
    normalized = " ".join(str(sanitize_for_logging(prompt)).strip().split())
    return normalized or "No prompt supplied."


def _title_for(prefix: str, goal: str) -> str:
    value = " ".join(goal.split())
    if len(value) > 56:
        value = value[:53].rstrip() + "..."
    return f"{prefix}: {value}"


def _codex_read_only_boundary() -> list[str]:
    return [
        "Chat drafts visible Harness records before any execution.",
        "Chat does not call Codex, providers, Docker, shell, browser, email, calendar, MCP, or mutate files directly.",
        "Execution requires a durable task, a daemon run-once lease, and registered adapter dispatch.",
        "Hosted-boundary approval is required before scoped context is sent to Codex.",
    ]
