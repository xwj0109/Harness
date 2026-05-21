from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from harness.cli.main import app


runner = CliRunner()


def test_governance_gates_json_is_read_only(tmp_path) -> None:
    result = runner.invoke(app, ["governance", "gates", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance.gate_registry/v1"
    assert any(gate["id"] == "no_protected_writes" for gate in payload["gates"])
    assert not (tmp_path / ".harness").exists()


def test_governance_gates_text_lists_core_gate() -> None:
    result = runner.invoke(app, ["governance", "gates"])

    assert result.exit_code == 0, result.output
    assert "Harness Governance Gates" in result.output
    assert "no_protected_writes" in result.output


class _FakeGovernanceTask:
    def __init__(self, task_id: str = "task_123") -> None:
        self.task = SimpleNamespace(id=task_id)
        self.governance = SimpleNamespace(
            status="active",
            agent="code_editor",
            branch="harness/task/demo",
            slug="demo",
            worktree_path="/tmp/demo",
            goal="demo goal",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "harness.governance_task/v1",
            "ok": True,
            "task": {"id": self.task.id},
            "governance": {
                "task_id": self.task.id,
                "status": self.governance.status,
                "agent": self.governance.agent,
                "branch": self.governance.branch,
                "slug": self.governance.slug,
            },
        }


def test_governance_tasks_create_cli_wires_service(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    calls = {}

    def fake_create(project_root, *, slug, agent_id, goal, base):
        calls.update({"project_root": project_root, "slug": slug, "agent_id": agent_id, "goal": goal, "base": base})
        return _FakeGovernanceTask()

    monkeypatch.setattr("harness.cli.main.create_governance_task", fake_create)

    result = runner.invoke(
        app,
        [
            "governance",
            "tasks",
            "create",
            "Demo",
            "--agent",
            "code_editor",
            "--goal",
            "demo goal",
            "--base",
            "main",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance_task/v1"
    assert payload["governance"]["task_id"] == "task_123"
    assert calls["slug"] == "Demo"
    assert calls["agent_id"] == "code_editor"
    assert calls["goal"] == "demo goal"
    assert calls["base"] == "main"


def test_governance_tasks_list_show_close_cli(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    fake = _FakeGovernanceTask()

    monkeypatch.setattr("harness.cli.main.list_governance_tasks", lambda project_root: [fake])
    monkeypatch.setattr("harness.cli.main.load_governance_task", lambda project_root, task_id: fake)
    monkeypatch.setattr("harness.cli.main.close_governance_task", lambda project_root, task_id: fake)

    listed = runner.invoke(app, ["governance", "tasks", "list", "--project", str(tmp_path), "--output", "json"])
    shown = runner.invoke(app, ["governance", "tasks", "show", "task_123", "--project", str(tmp_path), "--output", "json"])
    closed = runner.invoke(app, ["governance", "tasks", "close", "task_123", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    assert closed.exit_code == 0, closed.output
    assert json.loads(listed.output)["tasks"][0]["governance"]["task_id"] == "task_123"
    assert json.loads(shown.output)["governance"]["task_id"] == "task_123"
    assert json.loads(closed.output)["governance"]["task_id"] == "task_123"


def test_governance_context_build_cli_wires_service(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    calls = {}

    def fake_build(project_root, task_id):
        calls.update({"project_root": project_root, "task_id": task_id})
        return SimpleNamespace(
            path="/tmp/context.json",
            sha256="abc123",
            model_dump=lambda mode: {
                "schema_version": "harness.governance_context_pack/v1",
                "ok": True,
                "task_id": task_id,
                "path": "/tmp/context.json",
                "sha256": "abc123",
                "payload": {},
            },
        )

    monkeypatch.setattr("harness.cli.main.build_governance_context_pack", fake_build)

    result = runner.invoke(
        app,
        [
            "governance",
            "context",
            "build",
            "--task",
            "task_123",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance_context_pack/v1"
    assert payload["task_id"] == "task_123"
    assert payload["sha256"] == "abc123"
    assert calls["task_id"] == "task_123"


def test_governance_tests_plan_and_run_cli_wires_services(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    def fake_plan(project_root, task_id):
        return SimpleNamespace(
            task_id=task_id,
            task_type="governance_security",
            policy_hash="policy123",
            payload={"tests": []},
            model_dump=lambda mode: {
                "schema_version": "harness.governance_test_plan/v1",
                "ok": True,
                "task_id": task_id,
                "task_type": "governance_security",
                "policy_hash": "policy123",
                "payload": {"tests": []},
            },
        )

    def fake_run(project_root, task_id):
        return SimpleNamespace(
            ok=True,
            task_id=task_id,
            run_id="run123",
            status="pass",
            path="/tmp/test-run.json",
            policy_hash="policy123",
            model_dump=lambda mode: {
                "schema_version": "harness.governance_test_run/v1",
                "ok": True,
                "task_id": task_id,
                "run_id": "run123",
                "status": "pass",
                "path": "/tmp/test-run.json",
                "policy_hash": "policy123",
                "payload": {},
            },
        )

    monkeypatch.setattr("harness.cli.main.plan_governance_tests", fake_plan)
    monkeypatch.setattr("harness.cli.main.run_governance_tests", fake_run)

    planned = runner.invoke(app, ["governance", "tests", "plan", "task_123", "--project", str(tmp_path), "--output", "json"])
    ran = runner.invoke(app, ["governance", "tests", "run", "task_123", "--project", str(tmp_path), "--output", "json"])

    assert planned.exit_code == 0, planned.output
    assert ran.exit_code == 0, ran.output
    assert json.loads(planned.output)["schema_version"] == "harness.governance_test_plan/v1"
    assert json.loads(planned.output)["task_id"] == "task_123"
    assert json.loads(ran.output)["schema_version"] == "harness.governance_test_run/v1"
    assert json.loads(ran.output)["status"] == "pass"


def test_governance_merge_check_cli_maps_exit_codes(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    def fake_merge(project_root, *, branch, base, strict):
        return SimpleNamespace(
            payload={
                "schema_version": "harness.governance.merge_check/v1",
                "verdict": "request_changes",
                "summary": "needs review",
                "branch": branch,
                "base": base,
            },
            path=tmp_path / ".harness" / "governance" / "merge-check" / "run" / "verdict.json",
            exit_code=2,
        )

    monkeypatch.setattr("harness.cli.main.run_governance_merge_check", fake_merge)

    result = runner.invoke(
        app,
        [
            "governance",
            "merge-check",
            "feature",
            "--base",
            "main",
            "--strict",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance.merge_check/v1"
    assert payload["verdict"] == "request_changes"
    assert payload["branch"] == "feature"
