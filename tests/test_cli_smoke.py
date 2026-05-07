import json
import tomllib

from typer.testing import CliRunner

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexRunResult
from harness.models import BackendStatus
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_pyproject_exposes_harness_console_script() -> None:
    with open("pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)
    assert pyproject["project"]["scripts"]["harness"] == "harness.cli.main:app"


def test_cli_init_idempotent_and_backends(tmp_path) -> None:
    result1 = runner.invoke(app, ["init", "--project", str(tmp_path)])
    result2 = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result1.exit_code == 0
    assert result2.exit_code == 0
    assert (tmp_path / ".harness" / "config.yaml").exists()
    assert (tmp_path / ".harness" / "harness.sqlite").exists()
    assert (tmp_path / ".harness" / "runs").exists()
    assert (tmp_path / ".harness" / "tmp").exists()
    assert (tmp_path / ".harness" / "approvals.yaml").exists()
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("# Harness local artifacts") == 1
    assert gitignore.count(".harness/runs/") == 1
    assert gitignore.count(".harness/harness.sqlite") == 1
    assert gitignore.count(".harness/approvals.yaml") == 1
    assert gitignore.count(".harness/tmp/") == 1
    assert gitignore.count("*.egg-info/") == 1

    backends = runner.invoke(app, ["backends", "--project", str(tmp_path)])
    assert backends.exit_code == 0
    assert "codex_cli" in backends.output
    assert "local_openai_compatible" in backends.output


def test_cli_init_preserves_gitignore_and_does_not_duplicate_partial_entries(tmp_path) -> None:
    (tmp_path / ".gitignore").write_text("existing.log\n.harness/runs/\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "existing.log" in gitignore
    assert gitignore.count(".harness/runs/") == 1
    assert gitignore.count(".harness/harness.sqlite") == 1
    assert gitignore.count(".harness/approvals.yaml") == 1
    assert gitignore.count(".harness/tmp/") == 1
    assert gitignore.count("*.egg-info/") == 1


def test_cli_dev_create_run_runs_show(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]
    runs = runner.invoke(app, ["runs", "--project", str(tmp_path)])
    assert runs.exit_code == 0
    assert run_id in runs.output
    show = runner.invoke(app, ["show", run_id, "--project", str(tmp_path)])
    assert show.exit_code == 0
    assert "Final_report".lower().replace("_", "") not in show.output
    assert "final_report" in show.output
    run_dir = tmp_path / ".harness" / "runs" / run_id
    events_path = run_dir / "events.jsonl"
    transcript_path = run_dir / "transcript.jsonl"
    report_path = run_dir / "final_report.md"
    manifest_path = run_dir / "manifest.json"
    assert str(events_path) in show.output
    assert str(transcript_path) in show.output
    assert str(report_path) in show.output
    assert events_path.read_text(encoding="utf-8")
    assert transcript_path.exists()
    assert report_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "harness.manifest/v1"
    assert manifest["run_id"] == run_id
    assert manifest["run_mode"] == "dev"
    assert {artifact["kind"] for artifact in manifest["artifacts"]} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }


def test_cli_runs_and_show_support_json_output(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "json test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]

    runs = runner.invoke(app, ["runs", "--project", str(tmp_path), "--output", "json"])
    assert runs.exit_code == 0
    runs_payload = json.loads(runs.output)
    assert runs_payload["schema_version"] == "harness.runs/v1"
    assert runs_payload["runs"][0]["id"] == run_id
    assert runs_payload["runs"][0]["status"] == "completed"
    assert runs_payload["runs"][0]["task_type"] == "phase_1a_test"
    assert runs_payload["runs"][0]["backend_name"] is None

    show = runner.invoke(app, ["show", run_id, "--project", str(tmp_path), "--output", "json"])
    assert show.exit_code == 0
    show_payload = json.loads(show.output)
    assert show_payload["schema_version"] == "harness.manifest/v1"
    assert show_payload["run_id"] == run_id
    assert show_payload["run_mode"] == "dev"
    assert {artifact["kind"] for artifact in show_payload["artifacts"]} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }


def test_cli_runs_default_output_remains_text(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "text test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    runs = runner.invoke(app, ["runs", "--project", str(tmp_path)])
    assert runs.exit_code == 0
    assert not runs.output.lstrip().startswith("{")
    assert "\tcompleted\t" in runs.output


def test_cli_tasks_require_initialized_project(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Uninitialized task", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_objectives_require_initialized_project(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["objectives", "add", "--title", "Uninitialized objective", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_objectives_add_list_and_inspect_support_json_and_text(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    text_created = runner.invoke(
        app,
        ["objectives", "add", "--title", "Text objective", "--project", str(tmp_path)],
    )
    assert text_created.exit_code == 0
    assert "Created objective obj_" in text_created.output

    created = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Queue hardening",
            "--description",
            "Build objective persistence.",
            "--workbench",
            "coding",
            "--priority",
            "7",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert created.exit_code == 0
    created_payload = json.loads(created.output)
    assert created_payload["schema_version"] == "harness.objective/v1"
    assert created_payload["ok"] is True
    objective = created_payload["objective"]
    assert objective["id"].startswith("obj_")
    assert objective["status"] == "active"
    assert objective["title"] == "Queue hardening"
    assert objective["description"] == "Build objective persistence."
    assert objective["workbench_id"] == "coding"

    listed = runner.invoke(app, ["objectives", "list", "--project", str(tmp_path), "--output", "json"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.objectives/v1"
    assert [item["id"] for item in listed_payload["objectives"]][0] == objective["id"]

    text_listed = runner.invoke(app, ["objectives", "list", "--project", str(tmp_path)])
    assert text_listed.exit_code == 0
    assert f"{objective['id']}\tactive\t7\tQueue hardening" in text_listed.output

    inspected = runner.invoke(
        app,
        ["objectives", "inspect", objective["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["objective"]["id"] == objective["id"]

    text_inspected = runner.invoke(app, ["objectives", "inspect", objective["id"], "--project", str(tmp_path)])
    assert text_inspected.exit_code == 0
    assert f"Objective: {objective['id']}" in text_inspected.output
    assert "Workbench: coding" in text_inspected.output


def test_cli_objectives_reject_invalid_builtin_registry_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    invalid_workbench = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Bad workbench",
            "--workbench",
            "missing_workbench",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert invalid_workbench.exit_code != 0
    payload = json.loads(invalid_workbench.output)
    assert payload["schema_version"] == "harness.objective/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Workbench not found: missing_workbench"]


def test_cli_objectives_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("objectives must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("objectives must not preflight local backend")),
    )

    result = runner.invoke(
        app,
        ["objectives", "add", "--title", "Safe objective", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0
    serialized = json.dumps(json.loads(result.output))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_tasks_add_list_inspect_and_status_support_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    created = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Inspect repo",
            "--description",
            "Read repository state.",
            "--agent",
            "repo_inspector",
            "--workbench",
            "coding",
            "--priority",
            "7",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert created.exit_code == 0
    created_payload = json.loads(created.output)
    assert created_payload["schema_version"] == "harness.task/v1"
    assert created_payload["ok"] is True
    task = created_payload["task"]
    assert task["id"].startswith("task_")
    assert task["status"] == "ready"
    assert task["idempotency_key"].startswith("task_idem_")
    assert task["title"] == "Inspect repo"
    assert task["agent_id"] == "repo_inspector"
    assert task["workbench_id"] == "coding"
    assert task["spec_source_kind"] == "builtin"

    listed = runner.invoke(app, ["tasks", "list", "--project", str(tmp_path), "--output", "json"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.tasks/v1"
    assert [item["id"] for item in listed_payload["tasks"]] == [task["id"]]

    inspected = runner.invoke(
        app,
        ["tasks", "inspect", task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["task"]["id"] == task["id"]

    updated = runner.invoke(
        app,
        ["tasks", "status", task["id"], "succeeded", "--project", str(tmp_path), "--output", "json"],
    )
    assert updated.exit_code == 0
    assert json.loads(updated.output)["task"]["status"] == "succeeded"


def test_cli_tasks_support_objective_dependencies_approvals_and_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    objective_result = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Queue objective",
            "--workbench",
            "coding",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert objective_result.exit_code == 0
    objective_id = json.loads(objective_result.output)["objective"]["id"]

    upstream_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Upstream",
            "--objective",
            objective_id,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert upstream_result.exit_code == 0
    upstream_id = json.loads(upstream_result.output)["task"]["id"]

    downstream_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Downstream",
            "--objective",
            objective_id,
            "--depends-on",
            upstream_id,
            "--requires-approval",
            "hosted_provider",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert downstream_result.exit_code == 0
    downstream = json.loads(downstream_result.output)["task"]
    assert downstream["objective_id"] == objective_id
    assert downstream["status"] == "waiting_approval"
    assert downstream["depends_on"] == [upstream_id]
    assert downstream["required_approvals"] == ["hosted_provider"]

    listed = runner.invoke(
        app,
        ["tasks", "list", "--objective", objective_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert listed.exit_code == 0
    listed_tasks = json.loads(listed.output)["tasks"]
    assert {task["id"] for task in listed_tasks} == {upstream_id, downstream["id"]}

    graph = runner.invoke(
        app,
        ["tasks", "graph", "--objective", objective_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert graph.exit_code == 0
    graph_payload = json.loads(graph.output)
    assert graph_payload["schema_version"] == "harness.task_graph/v1"
    assert graph_payload["ok"] is True
    assert [objective["id"] for objective in graph_payload["objectives"]] == [objective_id]
    assert graph_payload["dependencies"][0]["upstream_task_id"] == upstream_id
    assert graph_payload["dependencies"][0]["downstream_task_id"] == downstream["id"]
    assert graph_payload["blocked_reasons"][downstream["id"]][0] == {
        "kind": "unsatisfied_dependency",
        "task_id": upstream_id,
        "status": "ready",
    }


def test_cli_tasks_reject_invalid_objective_and_dependency_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task = runner.invoke(
        app,
        ["tasks", "add", "--title", "Task", "--project", str(tmp_path), "--output", "json"],
    )
    assert task.exit_code == 0
    task_id = json.loads(task.output)["task"]["id"]

    bad_objective = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad objective",
            "--objective",
            "obj_missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    bad_dependency = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad dependency",
            "--depends-on",
            "task_missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    bad_filter = runner.invoke(
        app,
        ["tasks", "list", "--objective", "obj_missing", "--project", str(tmp_path), "--output", "json"],
    )
    cycle = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Cycle",
            "--depends-on",
            task_id,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert bad_objective.exit_code != 0
    assert json.loads(bad_objective.output)["errors"] == ["Objective not found: obj_missing"]
    assert bad_dependency.exit_code != 0
    assert json.loads(bad_dependency.output)["errors"] == ["Task not found: task_missing"]
    assert bad_filter.exit_code != 0
    assert json.loads(bad_filter.output)["errors"] == ["Objective not found: obj_missing"]
    assert cycle.exit_code == 0


def test_cli_tasks_cancel_and_retry_support_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    cancel_candidate = runner.invoke(
        app,
        ["tasks", "add", "--title", "Cancel me", "--project", str(tmp_path), "--output", "json"],
    )
    retry_candidate = runner.invoke(
        app,
        ["tasks", "add", "--title", "Retry me", "--project", str(tmp_path), "--output", "json"],
    )
    assert cancel_candidate.exit_code == 0
    assert retry_candidate.exit_code == 0
    cancel_task = json.loads(cancel_candidate.output)["task"]
    retry_task = json.loads(retry_candidate.output)["task"]
    retry_idempotency_key = retry_task["idempotency_key"]

    cancelled = runner.invoke(
        app,
        ["tasks", "cancel", cancel_task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert cancelled.exit_code == 0
    cancelled_payload = json.loads(cancelled.output)
    assert cancelled_payload["schema_version"] == "harness.task/v1"
    assert cancelled_payload["task"]["status"] == "cancelled"

    assert (
        runner.invoke(
            app,
            ["tasks", "status", retry_task["id"], "running", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["tasks", "status", retry_task["id"], "failed", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    retried = runner.invoke(
        app,
        ["tasks", "retry", retry_task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert retried.exit_code == 0
    retried_task = json.loads(retried.output)["task"]
    assert retried_task["status"] == "ready"
    assert retried_task["idempotency_key"] == retry_idempotency_key


def test_cli_tasks_cancel_and_retry_reject_invalid_states(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task_result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Terminal task", "--project", str(tmp_path), "--output", "json"],
    )
    assert task_result.exit_code == 0
    task_id = json.loads(task_result.output)["task"]["id"]

    retry_ready = runner.invoke(
        app,
        ["tasks", "retry", task_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert retry_ready.exit_code != 0
    assert json.loads(retry_ready.output)["errors"] == ["Task retry requires failed status: ready"]

    assert (
        runner.invoke(
            app,
            ["tasks", "status", task_id, "succeeded", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    cancel_succeeded = runner.invoke(
        app,
        ["tasks", "cancel", task_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert cancel_succeeded.exit_code != 0
    assert json.loads(cancel_succeeded.output)["errors"] == [
        "Invalid task transition: succeeded -> cancelled"
    ]


def test_cli_tasks_cancel_retry_require_initialized_project(tmp_path) -> None:
    for command in ["cancel", "retry"]:
        result = runner.invoke(
            app,
            ["tasks", command, "task_missing", "--project", str(tmp_path), "--output", "json"],
        )
        assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_tasks_cancel_retry_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight local backend")),
    )
    task_result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Lifecycle task", "--project", str(tmp_path), "--output", "json"],
    )
    task_id = json.loads(task_result.output)["task"]["id"]

    cancelled = runner.invoke(
        app,
        ["tasks", "cancel", task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert cancelled.exit_code == 0
    serialized = json.dumps(json.loads(cancelled.output))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_tasks_run_next_selects_task_without_creating_run_artifacts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    low = store.create_task(title="Low", priority=0)
    high = store.create_task(title="High", priority=10)

    result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task_run_next/v1"
    assert payload["ok"] is True
    assert payload["selected_task"]["id"] == high.id
    assert payload["selected_task"]["status"] == "leased"
    assert payload["attempt"]["task_id"] == high.id
    assert payload["attempt"]["status"] == "leased"
    assert payload["lease"]["task_id"] == high.id
    assert payload["lease"]["attempt_id"] == payload["attempt"]["id"]
    assert payload["lease"]["owner"] == "manual_cli"
    assert payload["lease"]["status"] == "active"
    assert store.get_task(low.id).status.value == "ready"
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())

    text_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path)])
    assert text_result.exit_code == 0
    assert text_result.output.startswith(f"Leased task {low.id}")


def test_cli_tasks_run_next_does_not_select_active_lease_twice(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    first = SQLiteStore(tmp_path).create_task(title="First", priority=10)
    second = SQLiteStore(tmp_path).create_task(title="Second", priority=5)

    first_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])
    second_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    first_payload = json.loads(first_result.output)
    second_payload = json.loads(second_result.output)
    assert first_payload["selected_task"]["id"] == first.id
    assert second_payload["selected_task"]["id"] == second.id
    assert second_payload["selected_task"]["id"] != first_payload["selected_task"]["id"]


def test_cli_tasks_run_next_returns_null_without_runnable_task(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task_run_next/v1"
    assert payload["ok"] is True
    assert payload["selected_task"] is None
    assert payload["attempt"] is None
    assert payload["lease"] is None


def test_cli_tasks_reject_invalid_builtin_registry_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    invalid_agent = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad agent",
            "--agent",
            "missing_agent",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    invalid_workbench = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad workbench",
            "--workbench",
            "missing_workbench",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert invalid_agent.exit_code != 0
    assert json.loads(invalid_agent.output)["errors"] == ["Agent not found: missing_agent"]
    assert invalid_workbench.exit_code != 0
    assert json.loads(invalid_workbench.output)["errors"] == ["Workbench not found: missing_workbench"]


def test_cli_tasks_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight local backend")),
    )

    result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Safe task", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_doctor_reports_initialized_project_without_mutation(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    before = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".harness").rglob("*"))

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    class FakeDockerVersion:
        returncode = 0
        stdout = "Docker version test\n"
        stderr = ""

    monkeypatch.setattr("harness.cli.main.subprocess.run", lambda *args, **kwargs: FakeDockerVersion())

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])
    after = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".harness").rglob("*"))

    assert result.exit_code == 0
    assert not result.output.lstrip().startswith("{")
    assert "Overall: pass" in result.output
    assert "pass\tinitialized" in result.output
    assert "pass\tconfig_loadable" in result.output
    assert before == after


def test_cli_doctor_uninitialized_project_fails_without_creating_harness_dir(tmp_path) -> None:
    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "fail\tinitialized" in result.output
    assert "fail\tconfig_loadable" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_doctor_supports_json_output_without_sensitive_backend_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: None)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.doctor/v1"
    assert payload["project_root"] == str(tmp_path.resolve())
    assert payload["ok"] is True
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["initialized"]["status"] == "pass"
    assert checks["config_loadable"]["status"] == "pass"
    assert checks["sandbox_safety"]["status"] == "pass"
    assert checks["docker_binary"]["status"] == "warn"
    paid_backend = next(
        backend
        for backend in checks["backend_preflight"]["details"]["backends"]
        if backend["name"] == "paid_openai_compatible"
    )
    assert paid_backend["status"] == "warn"
    assert paid_backend["reason"] == "Paid backend preflight skipped; disabled by default."
    serialized = json.dumps(payload)
    assert '"settings"' not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized


def test_cli_specs_registry_supports_json_output_without_runtime_leaks(tmp_path) -> None:
    result = runner.invoke(app, ["specs", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_registry/v1"
    assert {"local_reasoning", "codex_supervised"} <= set(payload["model_profiles"])
    assert {"repo_inspector", "code_editor", "test_runner", "quant_researcher", "job_researcher"} <= set(
        payload["agents"]
    )
    assert {"coding", "quant", "personal"} <= set(payload["workbenches"])
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_agent_supports_json_output() -> None:
    result = runner.invoke(app, ["specs", "agent", "repo_inspector", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.agent_spec/v1"
    assert payload["agent"]["id"] == "repo_inspector"
    assert payload["agent"]["kind"] == "specialist"
    assert payload["agent"]["model_profile"] == "local_reasoning"
    assert payload["agent"]["tool_policy"] == "read_only"
    assert payload["agent"]["memory_scope"] == "project"


def test_cli_specs_workbench_supports_json_output() -> None:
    result = runner.invoke(app, ["specs", "workbench", "coding", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.workbench_spec/v1"
    assert payload["workbench"]["id"] == "coding"
    assert payload["workbench"]["default_model_profile"] == "local_reasoning"
    assert {"repo_inspector", "code_editor", "test_runner"} <= set(payload["workbench"]["allowed_agents"])


def test_cli_specs_default_outputs_remain_text() -> None:
    registry = runner.invoke(app, ["specs"])
    agent = runner.invoke(app, ["specs", "agent", "repo_inspector"])
    workbench = runner.invoke(app, ["specs", "workbench", "coding"])

    assert registry.exit_code == 0
    assert not registry.output.lstrip().startswith("{")
    assert "Built-in specs:" in registry.output
    assert "repo_inspector" in registry.output
    assert "coding" in registry.output

    assert agent.exit_code == 0
    assert not agent.output.lstrip().startswith("{")
    assert "Agent: repo_inspector" in agent.output
    assert "Kind: specialist" in agent.output

    assert workbench.exit_code == 0
    assert not workbench.output.lstrip().startswith("{")
    assert "Workbench: coding" in workbench.output
    assert "Allowed agents: repo_inspector, code_editor, test_runner" in workbench.output


def test_cli_specs_missing_ids_fail_without_creating_harness_dir(tmp_path) -> None:
    missing_agent = runner.invoke(app, ["specs", "agent", "missing_agent"])
    missing_workbench = runner.invoke(app, ["specs", "workbench", "missing_workbench"])

    assert missing_agent.exit_code != 0
    assert "Agent not found: missing_agent" in missing_agent.output
    assert missing_workbench.exit_code != 0
    assert "Workbench not found: missing_workbench" in missing_workbench.output
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_valid_bundle_supports_text_and_json(tmp_path, monkeypatch) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v1",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
                "tool_policies": {
                    "read_only": {
                        "tools": {"repo_read": "allowed"},
                        "network": "forbidden",
                        "active_repo_write": "forbidden",
                        "hosted_boundary": "approval_required",
                    }
                },
                "memory_scopes": {"project": {"id": "project"}},
                "agents": {
                    "repo_inspector": {
                        "id": "repo_inspector",
                        "kind": "specialist",
                        "role": "Inspect repository evidence.",
                        "model_profile": "local_reasoning",
                        "tool_policy": "read_only",
                        "memory_scope": "project",
                    }
                },
                "workbenches": {
                    "coding": {
                        "id": "coding",
                        "description": "Coding workbench.",
                        "allowed_agents": ["repo_inspector"],
                        "default_model_profile": "local_reasoning",
                        "forbidden_actions": ["paid_api_fallback", "hosted_fallback"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not preflight local backend")),
    )

    text = runner.invoke(app, ["specs", "validate", str(bundle_path)])
    json_result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert text.exit_code == 0
    assert f"Spec bundle valid: {bundle_path.resolve()}" in text.output
    assert not text.output.lstrip().startswith("{")

    assert json_result.exit_code == 0
    payload = json.loads(json_result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is True
    assert payload["path"] == str(bundle_path.resolve())
    assert payload["errors"] == []
    assert payload["registry"]["agents"]["repo_inspector"]["kind"] == "specialist"
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_invalid_bundle_supports_text_and_json(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v1",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
                "workbenches": {
                    "coding": {
                        "id": "coding",
                        "description": "Coding workbench.",
                        "allowed_agents": ["missing_agent"],
                        "default_model_profile": "local_reasoning",
                        "forbidden_actions": ["paid_api_fallback", "hosted_fallback"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    text = runner.invoke(app, ["specs", "validate", str(bundle_path)])
    json_result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert text.exit_code != 0
    assert f"Spec bundle invalid: {bundle_path.resolve()}" in text.output
    assert "missing allowed agent" in text.output

    assert json_result.exit_code != 0
    payload = json.loads(json_result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert "missing allowed agent" in payload["errors"][0]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_missing_schema_version_supports_json_error(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Spec bundle missing schema_version."]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_unsupported_schema_version_supports_json_error(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v0",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]
    assert not (tmp_path / ".harness").exists()


def test_cli_run_read_only_repo_summary_with_mocked_local_backend(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                '{"command":"list_files","arguments":{"path":"."}}',
                '{"command":"final_answer","arguments":{"answer":"Local summary."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo and explain the structure",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code == 0
    assert "Created run" in result.output
    assert "Local summary." in result.output
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    report = tmp_path / ".harness" / "runs" / run_id / "final_report.md"
    assert "local_openai_compatible" in report.read_text(encoding="utf-8")


def test_cli_run_local_backend_unavailable_fails_with_guidance(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="Local OpenAI-compatible endpoint is unavailable. Start Ollama.",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code != 0
    assert "Local OpenAI-compatible endpoint is unavailable" in result.output


def test_cli_run_does_not_execute_codex_or_paid_fallback(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess_calls = []

    def forbidden_subprocess(*args, **kwargs):
        subprocess_calls.append(args)
        raise AssertionError("No subprocess commands should run for final-answer-only local backend.")

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return '{"command":"final_answer","arguments":{"answer":"No external fallback."}}'

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    monkeypatch.setattr("subprocess.run", forbidden_subprocess)
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code == 0
    assert not subprocess_calls
    assert "No external fallback." in result.output


def test_cli_backends_preflight_reports_codex_without_paid_preflight(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    result = runner.invoke(app, ["backends", "preflight", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "codex_cli:" in result.output
    assert "available: True" in result.output
    assert "Paid backend preflight skipped" in result.output


def test_cli_backends_support_json_output_without_settings(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["backends", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.backends/v1"
    names = {backend["name"] for backend in payload["backends"]}
    paid = next(backend for backend in payload["backends"] if backend["name"] == "paid_openai_compatible")

    assert {"codex_cli", "local_openai_compatible", "paid_openai_compatible"} <= names
    assert paid["constraints"] == ["disabled_by_default", "no_automatic_fallback", "preflight_skipped"]
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized


def test_cli_backends_preflight_supports_json_output(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    result = runner.invoke(app, ["backends", "preflight", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.backend_preflight/v1"
    by_name = {backend["name"]: backend for backend in payload["backends"]}

    assert by_name["codex_cli"]["available"] is True
    assert by_name["codex_cli"]["detected_capabilities"]["supports_exec"] is True
    assert by_name["local_openai_compatible"]["available"] is False
    assert by_name["local_openai_compatible"]["reason"] == "local unavailable"
    assert by_name["paid_openai_compatible"]["available"] is False
    assert by_name["paid_openai_compatible"]["reason"] == "Paid backend preflight skipped; disabled by default."


def test_cli_approvals_add_list_revoke(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    add = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning",
            "--duration-days",
            "30",
            "--project",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0
    approval_id = add.output.split("Created approval ", 1)[1].strip()
    listed = runner.invoke(app, ["approvals", "--project", str(tmp_path)])
    assert listed.exit_code == 0
    assert approval_id in listed.output
    revoked = runner.invoke(app, ["approvals", "revoke", approval_id, "--project", str(tmp_path)])
    assert revoked.exit_code == 0
    listed_after = runner.invoke(app, ["approvals", "--project", str(tmp_path)])
    assert "revoked=True" in listed_after.output


def test_cli_approvals_support_json_output(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    add = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning",
            "--duration-days",
            "30",
            "--project",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0
    approval_id = add.output.split("Created approval ", 1)[1].strip()
    assert runner.invoke(app, ["approvals", "revoke", approval_id, "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["approvals", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.approvals/v1"

    assert payload["approvals"][0]["id"] == approval_id
    assert payload["approvals"][0]["backend"] == "codex_cli"
    assert payload["approvals"][0]["data_boundary"] == "hosted_provider"
    assert payload["approvals"][0]["task_types"] == ["repo_planning"]
    assert payload["approvals"][0]["revoked"] is True


def test_cli_repo_planning_requires_hosted_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "run",
            "plan the safest fix",
            "--project",
            str(tmp_path),
            "--task-type",
            "repo_planning",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Hosted data-boundary approval required" in result.output
    assert "backend: codex_cli" in result.output
    assert "billing mode: subscription" in result.output
    assert "execution location: mixed" in result.output
    assert "data boundary: hosted_provider" in result.output
    assert "task type: repo_planning" in result.output
    assert f"project root: {tmp_path}" in result.output
    assert "data that may be sent:" in result.output
    assert "Hosted data-boundary approval denied." in result.output


def test_cli_repo_planning_uses_valid_approval_profile(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    approval = ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_days=30,
    )

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={"supports_exec": True, "supports_read_only_sandbox": True}
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            if final_message_path:
                final_message_path.write_text("Plan from Codex.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--sandbox", "read-only", "plan"],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Plan from Codex.",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "plan the safest fix",
            "--project",
            str(tmp_path),
            "--task-type",
            "repo_planning",
        ],
    )
    assert result.exit_code == 0
    assert approval.id in result.output
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    report = tmp_path / ".harness" / "runs" / run_id / "final_report.md"
    assert "Plan from Codex." in report.read_text(encoding="utf-8")


def test_existing_local_read_only_route_still_works(tmp_path, monkeypatch) -> None:
    test_cli_run_read_only_repo_summary_with_mocked_local_backend(tmp_path, monkeypatch)


def test_cli_simple_code_edit_routes_to_local_backend_only(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    def forbidden_codex(*args, **kwargs):
        raise AssertionError("simple_code_edit must not instantiate or execute Codex.")

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                '{"command":"final_answer","arguments":{"answer":"No patch."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    monkeypatch.setattr("harness.cli.main.CodexCliBackend", forbidden_codex)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
    )
    assert result.exit_code == 0
    assert "No patch." in result.output


def test_cli_simple_code_edit_denied_patch_is_not_applied(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
"""

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                json.dumps({"command": "apply_patch", "arguments": {"patch": patch}}),
                '{"command":"final_answer","arguments":{"answer":"Denied."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
        input="d\n",
    )
    assert result.exit_code == 0
    assert "Patch approval required:" in result.output
    assert "Denied." in result.output
    assert "value = 1" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cli_simple_code_edit_approved_patch_is_applied(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
""".replace("++++", "+++")

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                json.dumps({"command": "apply_patch", "arguments": {"patch": patch}}),
                '{"command":"final_answer","arguments":{"answer":"Approved."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
        input="a\n",
    )
    assert result.exit_code == 0
    assert "Patch approval required:" in result.output
    assert "Approved." in result.output
    assert "Changed files: app.py" in result.output
    assert "value = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
