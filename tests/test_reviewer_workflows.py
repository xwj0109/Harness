from __future__ import annotations

import json

import pytest

from harness.memory.sqlite_store import SQLiteStore
from harness.objective_runner import run_objective_autonomously
from harness.workflow_templates import WorkflowCheckpointTemplate, WorkflowTaskTemplate, WorkflowTemplate, template_for_intent


def test_coding_workflow_requires_implementation_review(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)

    stages = [task.metadata()["workflow_stage"] for task in template.tasks]
    assert stages == [
        "repo_planning",
        "codex_isolated_edit",
        "test_sandbox",
        "implementation_review",
        "security_review",
        "final_report",
    ]
    implementation_review = template.tasks[3]
    assert implementation_review.agent_id == "implementation_reviewer"
    assert implementation_review.execution_adapter == "review_gate"
    assert implementation_review.task_type == "implementation_review"
    assert implementation_review.depends_on_indexes == [2]
    assert implementation_review.metadata()["completion_gate"] is True
    assert implementation_review.agent_selection is not None
    assert implementation_review.agent_selection.required_kind == "reviewer"
    assert implementation_review.agent_selection.required_tool_policy_id == "read_only"
    assert implementation_review.agent_selection.required_outputs == ["implementation_review.md"]
    assert implementation_review.agent_selection.required_tags == ["review"]


def test_coding_workflow_requires_security_review(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)

    security_review = template.tasks[4]
    assert security_review.agent_id == "security_reviewer"
    assert security_review.execution_adapter == "review_gate"
    assert security_review.task_type == "security_review"
    assert security_review.depends_on_indexes == [3]
    assert security_review.metadata()["completion_gate"] is True
    assert security_review.metadata()["blocks_apply_back"] is True
    assert security_review.agent_selection is not None
    assert security_review.agent_selection.required_kind == "reviewer"
    assert security_review.agent_selection.required_tool_policy_id == "read_only"
    assert security_review.agent_selection.required_outputs == ["security_review.md"]
    assert security_review.agent_selection.required_tags == ["security"]


def test_research_workflow_requires_factuality_review(tmp_path) -> None:
    template = template_for_intent("research_brief", "research the repo architecture", tmp_path)

    assert [task.metadata()["workflow_stage"] for task in template.tasks] == [
        "read_only_inspection",
        "research_brief",
        "factuality_review",
        "research_synthesis",
    ]
    factuality_review = template.tasks[2]
    assert factuality_review.agent_id == "factuality_reviewer"
    assert factuality_review.execution_adapter == "review_gate"
    assert factuality_review.task_type == "factuality_review"
    assert factuality_review.depends_on_indexes == [1]
    assert factuality_review.metadata()["review_gate"] is True
    assert factuality_review.agent_selection is not None
    assert factuality_review.agent_selection.required_kind == "reviewer"
    assert factuality_review.agent_selection.required_outputs == ["factuality_review.md"]
    assert template.checkpoints[0].label == "Supervisor approval for reviewed research workflow"
    assert template.checkpoints[0].required is True


def test_workflow_template_payload_declares_agent_selection_requirements(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)
    payload = template.to_payload()

    selections = [task["agent_selection"] for task in payload["tasks"]]
    assert all(selection["schema_version"] == "harness.workflow_agent_selection/v1" for selection in selections)
    assert selections[0]["required_kind"] == "specialist"
    assert selections[0]["required_tool_policy_id"] == "read_only"
    assert selections[0]["required_outputs"] == ["repo_summary.md"]
    assert selections[1]["required_tool_policy_id"] == "isolated_code_edit"
    assert selections[2]["required_tool_policy_id"] == "docker_test"
    assert selections[3]["required_outputs"] == ["implementation_review.md"]
    assert selections[4]["required_tags"] == ["security"]
    assert selections[5]["required_kind"] == "orchestrator"


def test_review_failure_blocks_completion(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)

    final_report = template.tasks[5]
    assert final_report.depends_on_indexes == [0, 1, 2, 3, 4]
    assert template.tasks[3].metadata()["completion_gate"] is True
    assert template.tasks[4].metadata()["completion_gate"] is True
    assert final_report.metadata()["requires_evidence_links"] == "objective,task,run,artifact,policy"


def test_workflow_template_rejects_unknown_adapter_contract() -> None:
    with pytest.raises(ValueError, match="Unknown execution adapter: missing_adapter"):
        WorkflowTemplate(
            id="bad_adapter",
            interpreted_intent="bad_adapter",
            proposed_action="Invalid template",
            objective_title="Invalid",
            objective_description="Invalid",
            tasks=[
                WorkflowTaskTemplate(
                    title="Bad adapter",
                    description="Invalid adapter metadata should fail before records are created.",
                    execution_adapter="missing_adapter",
                    task_type="phase_1a_test",
                )
            ],
        )


def test_workflow_template_rejects_empty_task_list() -> None:
    with pytest.raises(ValueError, match="template must contain at least one task"):
        WorkflowTemplate(
            id="empty",
            interpreted_intent="empty",
            proposed_action="Invalid template",
            objective_title="Invalid",
            objective_description="Invalid",
            tasks=[],
        )


def test_workflow_template_rejects_forward_dependency() -> None:
    with pytest.raises(ValueError, match="depends_on_indexes must reference earlier task indexes"):
        WorkflowTemplate(
            id="bad_dependency",
            interpreted_intent="bad_dependency",
            proposed_action="Invalid template",
            objective_title="Invalid",
            objective_description="Invalid",
            tasks=[
                WorkflowTaskTemplate(
                    title="Future dependency",
                    description="Forward dependencies should not be representable in workflow templates.",
                    execution_adapter="dry_run",
                    task_type="phase_1a_test",
                    depends_on_indexes=[1],
                ),
                WorkflowTaskTemplate(
                    title="Later task",
                    description="Later task.",
                    execution_adapter="dry_run",
                    task_type="phase_1a_test",
                ),
            ],
        )


def test_workflow_template_rejects_invalid_checkpoint_contract() -> None:
    with pytest.raises(ValueError, match="checkpoint 0 label is required"):
        WorkflowTemplate(
            id="bad_checkpoint",
            interpreted_intent="bad_checkpoint",
            proposed_action="Invalid template",
            objective_title="Invalid",
            objective_description="Invalid",
            tasks=[
                WorkflowTaskTemplate(
                    title="Valid dry run",
                    description="Valid task.",
                    execution_adapter="dry_run",
                    task_type="phase_1a_test",
                )
            ],
            checkpoints=[
                WorkflowCheckpointTemplate(
                    label="",
                    reason="Missing label should fail before records are created.",
                )
            ],
        )


def test_workflow_template_rejects_review_gate_targeting_missing_prior_stage() -> None:
    with pytest.raises(ValueError, match="review_target_stage=missing_stage must match a prior workflow_stage"):
        WorkflowTemplate(
            id="bad_review_target",
            interpreted_intent="bad_review_target",
            proposed_action="Invalid template",
            objective_title="Invalid",
            objective_description="Invalid",
            tasks=[
                WorkflowTaskTemplate(
                    title="Evidence",
                    description="Local evidence.",
                    execution_adapter="dry_run",
                    task_type="phase_1a_test",
                    task_metadata={"workflow_stage": "evidence"},
                ),
                WorkflowTaskTemplate(
                    title="Implementation review",
                    description="Review gate cannot target a stage absent from the prior graph.",
                    execution_adapter="review_gate",
                    task_type="implementation_review",
                    agent_id="implementation_reviewer",
                    depends_on_indexes=[0],
                    task_metadata={
                        "workflow_stage": "implementation_review",
                        "review_role": "implementation_reviewer",
                        "review_gate": True,
                        "completion_gate": True,
                        "review_target_stage": "missing_stage",
                    },
                ),
            ],
        )


def test_review_gate_adapter_writes_typed_upstream_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Review objective")
    upstream = store.create_task(
        title="Record sandbox test evidence",
        objective_id=objective.id,
        agent_id="test_runner",
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "test_sandbox",
        },
    )
    review = store.create_task(
        title="Implementation review",
        objective_id=objective.id,
        agent_id="implementation_reviewer",
        depends_on=[upstream.id],
        metadata={
            "execution_adapter": "review_gate",
            "task_type": "implementation_review",
            "workflow_stage": "implementation_review",
            "review_role": "implementation_reviewer",
            "review_gate": True,
            "completion_gate": True,
            "review_target_stage": "test_sandbox",
        },
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local", max_steps=2)

    assert result.ok is True
    assert result.stop_reason == "objective_succeeded"
    assert [step.adapter_id for step in result.step_results] == ["dry_run", "review_gate"]
    fresh = SQLiteStore(tmp_path)
    review_task = fresh.get_task(review.id)
    assert review_task.status.value == "succeeded"
    assert review_task.run_id is not None
    artifacts = fresh.list_artifacts(review_task.run_id)
    report_artifact = next(artifact for artifact in artifacts if artifact.kind == "review_report")
    report = json.loads(report_artifact.path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "harness.review_gate_report/v1"
    assert report["verdict"] == "passed"
    assert report["review_role"] == "implementation_reviewer"
    assert report["upstream_tasks"][0]["task_id"] == upstream.id
    assert "final_report" in report["upstream_tasks"][0]["artifact_kinds"]
    assert any(check["id"] == "dependency_evidence" and check["status"] == "passed" for check in report["checks"])
    assert any(
        check["id"] == "review_target_stage_evidence" and check["status"] == "passed"
        for check in report["checks"]
    )


def test_review_gate_adapter_accepts_transitive_target_stage_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Review objective")
    target = store.create_task(
        title="Prepare isolated edit",
        objective_id=objective.id,
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "codex_isolated_edit",
        },
    )
    intermediate = store.create_task(
        title="Record sandbox test evidence",
        objective_id=objective.id,
        depends_on=[target.id],
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "test_sandbox",
        },
    )
    review = store.create_task(
        title="Implementation review",
        objective_id=objective.id,
        agent_id="implementation_reviewer",
        depends_on=[intermediate.id],
        metadata={
            "execution_adapter": "review_gate",
            "task_type": "implementation_review",
            "workflow_stage": "implementation_review",
            "review_role": "implementation_reviewer",
            "review_gate": True,
            "completion_gate": True,
            "review_target_stage": "codex_isolated_edit",
        },
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local", max_steps=3)

    assert result.ok is True
    assert [step.adapter_id for step in result.step_results] == ["dry_run", "dry_run", "review_gate"]
    fresh = SQLiteStore(tmp_path)
    review_task = fresh.get_task(review.id)
    report_artifact = next(artifact for artifact in fresh.list_artifacts(review_task.run_id) if artifact.kind == "review_report")
    report = json.loads(report_artifact.path.read_text(encoding="utf-8"))
    assert report["verdict"] == "passed"
    assert report["target_stage_tasks"][0]["task_id"] == target.id
    assert report["target_stage_tasks"][0]["workflow_stage"] == "codex_isolated_edit"
    assert any(
        check["id"] == "review_target_stage_evidence" and check["status"] == "passed"
        for check in report["checks"]
    )
    final_report = next(artifact for artifact in fresh.list_artifacts(review_task.run_id) if artifact.kind == "final_report")
    assert "## Target Stage Evidence" in final_report.path.read_text(encoding="utf-8")


def test_review_gate_adapter_fails_when_target_stage_absent_from_upstream_graph(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Review objective")
    upstream = store.create_task(
        title="Record unrelated evidence",
        objective_id=objective.id,
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "test_sandbox",
        },
    )
    review = store.create_task(
        title="Implementation review",
        objective_id=objective.id,
        agent_id="implementation_reviewer",
        depends_on=[upstream.id],
        metadata={
            "execution_adapter": "review_gate",
            "task_type": "implementation_review",
            "workflow_stage": "implementation_review",
            "review_role": "implementation_reviewer",
            "review_gate": True,
            "completion_gate": True,
            "review_target_stage": "codex_isolated_edit",
        },
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local", max_steps=2)

    assert result.ok is False
    assert result.stop_reason == "execution_failed"
    fresh = SQLiteStore(tmp_path)
    review_task = fresh.get_task(review.id)
    assert review_task.status.value == "failed"
    report_artifact = next(artifact for artifact in fresh.list_artifacts(review_task.run_id) if artifact.kind == "review_report")
    report = json.loads(report_artifact.path.read_text(encoding="utf-8"))
    target_check = next(check for check in report["checks"] if check["id"] == "review_target_stage_evidence")
    assert target_check["status"] == "failed"
    assert target_check["reasons"] == ["Review target stage has no upstream evidence: codex_isolated_edit."]


def test_review_gate_adapter_rejects_wrong_reviewer_contract_at_creation(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Review objective")
    upstream = store.create_task(
        title="Record sandbox test evidence",
        objective_id=objective.id,
        agent_id="test_runner",
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "test_sandbox",
        },
    )
    with pytest.raises(ValueError, match="review_role=implementation_reviewer"):
        store.create_task(
            title="Implementation review",
            objective_id=objective.id,
            agent_id="implementation_reviewer",
            depends_on=[upstream.id],
            metadata={
                "execution_adapter": "review_gate",
                "task_type": "implementation_review",
                "workflow_stage": "implementation_review",
                "review_role": "security_reviewer",
                "review_gate": True,
                "completion_gate": True,
                "review_target_stage": "test_sandbox",
            },
        )
