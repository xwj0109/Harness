from __future__ import annotations

from harness.workflow_templates import template_for_intent


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
    assert implementation_review.execution_adapter == "dry_run"
    assert implementation_review.depends_on_indexes == [2]
    assert implementation_review.metadata()["completion_gate"] is True


def test_coding_workflow_requires_security_review(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)

    security_review = template.tasks[4]
    assert security_review.agent_id == "security_reviewer"
    assert security_review.execution_adapter == "dry_run"
    assert security_review.depends_on_indexes == [3]
    assert security_review.metadata()["completion_gate"] is True
    assert security_review.metadata()["blocks_apply_back"] is True


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
    assert factuality_review.depends_on_indexes == [1]
    assert factuality_review.metadata()["review_gate"] is True


def test_review_failure_blocks_completion(tmp_path) -> None:
    template = template_for_intent("coding_fix", "fix failing tests", tmp_path)

    final_report = template.tasks[5]
    assert final_report.depends_on_indexes == [0, 1, 2, 3, 4]
    assert template.tasks[3].metadata()["completion_gate"] is True
    assert template.tasks[4].metadata()["completion_gate"] is True
    assert final_report.metadata()["requires_evidence_links"] == "objective,task,run,artifact,policy"
