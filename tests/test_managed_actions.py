from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.action_executors import execute_managed_action
from harness.action_policy import decide_managed_action
from harness.action_router import ManagedActionDecisionStatus, ManagedActionRisk, route_managed_action
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_managed_action_router_routes_empty_markdown_defaults() -> None:
    route = route_managed_action("create an empty .md file in this directory")

    assert route.intent == "create_empty_markdown_file"
    assert route.executor == "create_empty_file"
    assert route.normalized_arguments["filename"] == "scratch.md"
    assert route.normalized_arguments["allowed_extensions"] == [".md"]


def test_managed_action_router_extracts_named_markdown_file() -> None:
    route = route_managed_action("do scratch.md")

    assert route.intent == "create_empty_markdown_file"
    assert route.confidence == "exact"
    assert route.normalized_arguments["filename"] == "scratch.md"


def test_managed_action_router_routes_file_content_write_before_empty_file_creation() -> None:
    route = route_managed_action("write in side scratch.md 'hello'")

    assert route.intent == "write_file"
    assert route.executor == "write_file"
    assert route.normalized_arguments["filename"] == "scratch.md"
    assert route.normalized_arguments["text"] == "hello"


def test_managed_action_router_routes_plain_language_file_writes() -> None:
    write_to = route_managed_action("write hello world to scratch.md")
    create_with = route_managed_action("create scratch.md with hello world")

    assert write_to.intent == "write_file"
    assert write_to.normalized_arguments["filename"] == "scratch.md"
    assert write_to.normalized_arguments["text"] == "hello world"
    assert create_with.intent == "write_file"
    assert create_with.normalized_arguments["filename"] == "scratch.md"
    assert create_with.normalized_arguments["text"] == "hello world"


def test_managed_action_router_routes_text_file_directory_and_note() -> None:
    text = route_managed_action("create empty todo.txt")
    directory = route_managed_action("create folder called notes")
    note = route_managed_action("write note that prefer local evidence")

    assert text.intent == "create_empty_text_file"
    assert text.normalized_arguments["filename"] == "todo.txt"
    assert directory.intent == "create_directory"
    assert directory.normalized_arguments["dirname"] == "notes"
    assert note.intent == "local_note"
    assert note.normalized_arguments["text"] == "prefer local evidence"


def test_managed_action_router_routes_simple_python_script_before_directory_phrase() -> None:
    route = route_managed_action("create a simple code in python in this directory that prints a simple sentence")

    assert route.intent == "create_python_script"
    assert route.executor == "create_file_with_content"
    assert route.normalized_arguments["filename"] == "simple_script.py"
    assert route.normalized_arguments["text"] == "print('Hello from Harness.')"
    assert route.normalized_arguments["allowed_extensions"] == [".py"]


def test_managed_action_router_routes_black_scholes_python_script() -> None:
    route = route_managed_action("create a python script for the black scholes pricing")

    assert route.intent == "create_python_script"
    assert route.executor == "create_file_with_content"
    assert route.normalized_arguments["filename"] == "black_scholes_pricing.py"
    assert "def black_scholes_price(" in route.normalized_arguments["text"]
    assert "math.erf" in route.normalized_arguments["text"]
    assert route.required_approvals == []


def test_managed_action_router_routes_sandboxed_tests_as_approval_required(tmp_path) -> None:
    route = route_managed_action("run the tests")
    decision = decide_managed_action(route, tmp_path)

    assert route.intent == "run_tests"
    assert route.executor == "run_tests"
    assert route.required_approvals == ["docker_execution"]
    assert decision.status == ManagedActionDecisionStatus.APPROVAL_REQUIRED
    assert decision.requires_human is True


def test_managed_action_router_unsupported_fallback() -> None:
    route = route_managed_action("ship the whole product")

    assert route.intent == "unsupported"
    assert route.executor == "none"


def test_managed_action_policy_allows_low_risk_and_denies_forbidden_paths(tmp_path) -> None:
    allowed = route_managed_action("create an empty .md file")
    denied_git = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": ".git/secret.md"}})
    denied_harness = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": ".harness/foo.md"}})
    denied_parent = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": "../secret.md"}})
    denied_absolute = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": str(tmp_path / "absolute.md")}})
    denied_secret = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": "token"}})
    denied_extension = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": "scratch.py"}})
    denied_backslash = allowed.model_copy(update={"normalized_arguments": {**allowed.normalized_arguments, "filename": r"docs\scratch.md"}})

    allowed_decision = decide_managed_action(allowed, tmp_path)
    assert allowed_decision.status == ManagedActionDecisionStatus.AUTO_ALLOWED
    assert allowed_decision.sandbox_assessment is not None
    assert allowed_decision.sandbox_assessment.status == "safe"
    assert allowed_decision.sandbox_assessment.dangerous is False
    assert allowed_decision.sandbox_assessment.expected_paths == ["scratch.md"]
    assert decide_managed_action(denied_git, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_harness, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_parent, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_absolute, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_secret, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_extension, tmp_path).status == ManagedActionDecisionStatus.DENIED
    assert decide_managed_action(denied_backslash, tmp_path).status == ManagedActionDecisionStatus.DENIED


def test_managed_action_policy_denies_file_write_to_existing_directory(tmp_path) -> None:
    (tmp_path / "scratch.md").mkdir()
    route = route_managed_action("write hello to scratch.md")

    decision = decide_managed_action(route, tmp_path)

    assert decision.status == ManagedActionDecisionStatus.DENIED
    assert decision.sandbox_assessment is not None
    assert decision.sandbox_assessment.status == "dangerous"
    assert "existing directory" in " ".join(decision.sandbox_assessment.reasons)


def test_managed_action_policy_denies_when_sandbox_preflight_finds_dangerous_executor(tmp_path) -> None:
    route = route_managed_action("create an empty .md file").model_copy(update={"executor": "unknown"})

    decision = decide_managed_action(route, tmp_path)

    assert decision.status == ManagedActionDecisionStatus.DENIED
    assert decision.sandbox_assessment is not None
    assert decision.sandbox_assessment.status == "dangerous"
    assert decision.sandbox_assessment.dangerous is True
    assert "Sandbox preflight classified the action as dangerous." in decision.reasons
    assert "does not support executor" in " ".join(decision.sandbox_assessment.reasons)
    assert not (tmp_path / "scratch.md").exists()


def test_managed_action_policy_never_auto_allows_destructive_routes(tmp_path) -> None:
    route = route_managed_action("create an empty .md file").model_copy(
        update={
            "intent": "delete_file",
            "risk": ManagedActionRisk.DESTRUCTIVE,
            "executor": "delete_file",
        }
    )

    decision = decide_managed_action(route, tmp_path)

    assert decision.status == ManagedActionDecisionStatus.DENIED
    assert "destructive" in " ".join(decision.reasons)


def test_managed_action_executor_creates_file_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("create an empty .md file")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.ok is True
    assert result.run_id is not None
    assert (tmp_path / "scratch.md").exists()
    assert result.report_path is not None
    assert result.report_path.exists()
    run = store.get_run(result.run_id)
    assert run.status == "succeeded"
    assert {artifact.kind for artifact in store.verify_artifacts(result.run_id)} >= {"created_file", "final_report"}


def test_managed_action_executor_requires_auto_allowed_decision(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("run the tests")
    decision = decide_managed_action(route, tmp_path)

    try:
        execute_managed_action(tmp_path, route, decision, store)
    except ValueError as exc:
        assert "not auto-allowed" in str(exc)
    else:
        raise AssertionError("expected non-auto-allowed action to be refused")


def test_managed_action_executor_requires_decision_to_match_route(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("create an empty .md file")
    decision = decide_managed_action(route, tmp_path)
    tampered_route = route.model_copy(update={"normalized_arguments": {**route.normalized_arguments, "filename": ".harness/foo.md"}})

    try:
        execute_managed_action(tmp_path, tampered_route, decision, store)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected mismatched decision and route to be refused")
    assert not (tmp_path / ".harness" / "foo.md").exists()


def test_managed_action_executor_does_not_overwrite(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / "scratch.md").write_text("keep\n", encoding="utf-8")
    route = route_managed_action("do scratch.md")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert (tmp_path / "scratch.md").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "scratch-2.md").exists()
    assert result.created_paths == [tmp_path / "scratch-2.md"]


def test_managed_action_executor_creates_missing_parent_for_empty_file(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("do docs/scratch.md")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.ok is True
    assert (tmp_path / "docs" / "scratch.md").exists()
    assert result.created_paths == [tmp_path / "docs" / "scratch.md"]


def test_managed_action_executor_writes_content_to_existing_file(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / "scratch.md").write_text("", encoding="utf-8")
    route = route_managed_action("write in side scratch.md 'hello'")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.ok is True
    assert result.intent == "write_file"
    assert (tmp_path / "scratch.md").read_text(encoding="utf-8") == "hello\n"
    assert not (tmp_path / "scratch-2.md").exists()
    assert result.changed_paths == [tmp_path / "scratch.md"]
    assert {artifact.kind for artifact in store.verify_artifacts(result.run_id)} >= {"changed_file", "final_report"}


def test_managed_action_executor_creates_simple_python_script(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("create a simple code in python in this directory that prints a simple sentence")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.ok is True
    assert result.intent == "create_python_script"
    assert (tmp_path / "simple_script.py").read_text(encoding="utf-8") == "print('Hello from Harness.')\n"
    assert not (tmp_path / "that").exists()
    assert result.created_paths == [tmp_path / "simple_script.py"]
    assert {artifact.kind for artifact in store.verify_artifacts(result.run_id)} >= {"created_file", "final_report"}


def test_managed_action_executor_creates_black_scholes_python_script(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("create a python script for the black scholes pricing")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    script = tmp_path / "black_scholes_pricing.py"
    assert result.ok is True
    assert result.created_paths == [script]
    content = script.read_text(encoding="utf-8")
    assert "def black_scholes_price(" in content
    assert "argparse.ArgumentParser" in content
    assert {artifact.kind for artifact in store.verify_artifacts(result.run_id)} >= {"created_file", "final_report"}


def test_managed_action_executor_preserves_relative_parent_when_avoiding_overwrite(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "scratch.md").write_text("keep\n", encoding="utf-8")
    route = route_managed_action("do docs/scratch.md")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert (tmp_path / "docs" / "scratch.md").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "docs" / "scratch-2.md").exists()
    assert not (tmp_path / "scratch-2.md").exists()
    assert result.created_paths == [tmp_path / "docs" / "scratch-2.md"]


def test_managed_action_executor_directory_existing_directory_noops(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / "notes").mkdir()
    route = route_managed_action("create folder called notes")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.ok is True
    assert result.created_paths == []
    assert result.message == "Directory already exists `notes`."
    assert result.report_path is not None
    assert result.report_path.exists()


def test_managed_action_executor_directory_refuses_existing_file_target(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / "notes").write_text("not a directory\n", encoding="utf-8")
    route = route_managed_action("create folder called notes")
    decision = decide_managed_action(route, tmp_path)

    assert decision.status == ManagedActionDecisionStatus.DENIED
    assert "non-directory target" in " ".join(decision.reasons)
    try:
        execute_managed_action(tmp_path, route, decision, store)
    except ValueError as exc:
        assert "not auto-allowed" in str(exc)
    else:
        raise AssertionError("expected existing file target to be refused")


def test_managed_action_report_contains_release_ready_sections(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    route = route_managed_action("create an empty .md file")
    decision = decide_managed_action(route, tmp_path)

    result = execute_managed_action(tmp_path, route, decision, store)

    assert result.report_path is not None
    content = result.report_path.read_text(encoding="utf-8")
    for heading in (
        "# Harness Managed Action Report",
        "## Summary",
        "## Result",
        "## Policy",
        "## Evidence",
        "## Next Actions",
    ):
        assert heading in content
    assert "- Intent: create_empty_markdown_file" in content
    assert "- Decision: auto_allowed" in content
    assert "- Sandbox preflight: safe" in content


def test_cli_actions_route_is_read_only(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["actions", "route", "create an empty .md file", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.managed_action_route_preview/v1"
    assert payload["route"]["intent"] == "create_empty_markdown_file"
    assert payload["decision"]["status"] == "auto_allowed"
    assert not (tmp_path / "scratch.md").exists()


def test_cli_actions_run_executes_auto_allowed_action_and_report(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        ["actions", "run", "create an empty .md file", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.managed_action_run/v1"
    assert payload["ok"] is True
    assert (tmp_path / "scratch.md").exists()
    run_id = payload["result"]["run_id"]
    assert run_id
    assert payload["result"]["report_path"].endswith("final_report.md")

    report = runner.invoke(app, ["actions", "report", run_id, "--project", str(tmp_path), "--output", "json"])
    assert report.exit_code == 0, report.output
    report_payload = json.loads(report.output)
    assert report_payload["schema_version"] == "harness.managed_action_report/v1"
    assert "# Harness Managed Action Report" in report_payload["content"]


def test_cli_actions_run_refuses_unsafe_path(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        ["actions", "run", "create ../secret.md", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["decision"]["status"] in {"denied", "unsupported"}
    assert not (tmp_path.parent / "secret.md").exists()


def test_cli_actions_run_refuses_approval_required_route(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        ["actions", "run", "run the tests", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["route"]["intent"] == "run_tests"
    assert payload["decision"]["status"] == "approval_required"
    assert not list((tmp_path / ".harness" / "runs").glob("run_*"))
