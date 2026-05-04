from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.sandbox import DockerRunResult, DockerSandboxConfig
from harness.test_runner import DockerTestRunner, TestRunDecision, summarize_test_output_for_model


runner = CliRunner()


class FakeDockerRunner:
    created_workspaces: list[Path] = []
    run_calls: list[tuple[Path, list[str]]] = []
    run_workdirs: list[str | None] = []
    preflight_calls = 0

    def __init__(self, config):
        if isinstance(config, DockerSandboxConfig):
            self.config = config
        elif hasattr(config, "model_dump"):
            self.config = DockerSandboxConfig.model_validate(config.model_dump())
        else:
            self.config = DockerSandboxConfig.model_validate(config)

    def preflight(self):
        FakeDockerRunner.preflight_calls += 1

    def create_workspace(self, project_root):
        from harness.sandbox import DockerSandboxRunner

        workspace = DockerSandboxRunner(DockerSandboxConfig()).create_workspace(project_root)
        FakeDockerRunner.created_workspaces.append(workspace.path)
        return workspace

    def run(self, workspace, command, timeout_seconds=None, workdir=None):
        FakeDockerRunner.run_calls.append((workspace.path, list(command)))
        FakeDockerRunner.run_workdirs.append(workdir)
        (workspace.path / ".pytest_cache").mkdir()
        (workspace.path / ".pytest_cache" / "README").write_text("cache\n", encoding="utf-8")
        (workspace.path / "__pycache__").mkdir()
        (workspace.path / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (workspace.path / "tmp_output.txt").write_text("output\n", encoding="utf-8")
        return DockerRunResult(
            ok=True,
            exit_code=0,
            stdout="passed",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            image=self.config.image,
            command=list(command),
            workdir=self.config.workdir,
        )


def reset_fake() -> None:
    FakeDockerRunner.created_workspaces = []
    FakeDockerRunner.run_calls = []
    FakeDockerRunner.run_workdirs = []
    FakeDockerRunner.preflight_calls = 0


def test_cli_tests_run_approval_executes_docker_and_writes_artifacts(tmp_path, monkeypatch) -> None:
    reset_fake()
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("harness.test_runner.DockerSandboxRunner", FakeDockerRunner)

    result = runner.invoke(
        app,
        ["tests", "run", "--project", str(tmp_path), "--", "python", "-m", "pytest", "-q"],
        input="a\n",
    )

    assert result.exit_code == 0
    assert "Status: tests_passed" in result.output
    assert FakeDockerRunner.run_calls
    workspace, command = FakeDockerRunner.run_calls[0]
    assert command == ["python", "-m", "pytest", "-q"]
    assert workspace != tmp_path
    assert tmp_path not in workspace.parents
    assert not workspace.exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / "__pycache__").exists()
    assert not (tmp_path / "tmp_output.txt").exists()
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    run_dir = tmp_path / ".harness" / "runs" / run_id
    assert (run_dir / "test_stdout.txt").read_text(encoding="utf-8") == "passed"
    assert "tests_passed" in (run_dir / "test_result.json").read_text(encoding="utf-8")
    assert "Tests passed." in (run_dir / "final_report.md").read_text(encoding="utf-8")


def test_cli_tests_run_denial_does_not_call_docker_run_and_cleans_workspace(tmp_path, monkeypatch) -> None:
    reset_fake()
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("harness.test_runner.DockerSandboxRunner", FakeDockerRunner)

    result = runner.invoke(
        app,
        ["tests", "run", "--project", str(tmp_path), "--", "pytest", "-q"],
        input="d\n",
    )

    assert result.exit_code == 0
    assert "Status: execution_denied" in result.output
    assert FakeDockerRunner.preflight_calls == 1
    assert FakeDockerRunner.run_calls == []
    assert FakeDockerRunner.created_workspaces
    assert not FakeDockerRunner.created_workspaces[0].exists()
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    events = (tmp_path / ".harness" / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
    assert "test_execution_decision" in events
    assert "denied" in events


def test_run_in_existing_run_writes_suffixed_artifacts_and_uses_cwd(tmp_path) -> None:
    reset_fake()
    (tmp_path / "tests").mkdir()
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="edit", task_type="simple_code_edit", status="running")
    cfg = default_config()
    runner_obj = DockerTestRunner(
        tmp_path,
        cfg,
        store,
        StaticApproval("approved"),
        docker_runner=FakeDockerRunner(cfg.sandbox),
    )

    first = runner_obj.run_in_existing_run(run.id, ["pytest", "-q"], cwd="tests", artifact_index=1)
    second = runner_obj.run_in_existing_run(run.id, ["pytest", "-q"], cwd="tests", artifact_index=2)

    run_dir = tmp_path / ".harness" / "runs" / run.id
    assert first["stdout_artifact"] == str(run_dir / "test_stdout.txt")
    assert second["stdout_artifact"] == str(run_dir / "test_stdout_2.txt")
    assert (run_dir / "test_result.json").exists()
    assert (run_dir / "test_result_2.json").exists()
    assert FakeDockerRunner.run_workdirs == ["/workspace/tests", "/workspace/tests"]
    assert store.get_run(run.id).status == "running"


def test_cli_tests_run_rejects_empty_and_metacharacters(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    empty = runner.invoke(app, ["tests", "run", "--project", str(tmp_path), "--"])
    assert empty.exit_code != 0
    bad = runner.invoke(app, ["tests", "run", "--project", str(tmp_path), "--", "pytest", "&&", "echo"])
    assert bad.exit_code != 0
    assert "Shell metacharacters are not allowed" in bad.output


def test_cli_tests_run_does_not_instantiate_codex_or_paid_or_local(tmp_path, monkeypatch) -> None:
    reset_fake()
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr("harness.test_runner.DockerSandboxRunner", FakeDockerRunner)

    def forbidden(*args, **kwargs):
        raise AssertionError("No model backends should be instantiated for Docker tests.")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", forbidden)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", forbidden)
    result = runner.invoke(
        app,
        ["tests", "run", "--project", str(tmp_path), "--", "pytest", "-q"],
        input="d\n",
    )
    assert result.exit_code == 0


class StaticApproval:
    def __init__(self, decision: str = "approved"):
        self.decision = decision

    def decide(self, details: str) -> TestRunDecision:
        return TestRunDecision(decision=self.decision)


class ResultDockerRunner(FakeDockerRunner):
    def __init__(self, config, result: DockerRunResult):
        super().__init__(config)
        self.result = result

    def run(self, workspace, command, timeout_seconds=None, workdir=None):
        return self.result


def run_with_result(tmp_path, docker_result: DockerRunResult):
    store = SQLiteStore(tmp_path)
    store.initialize()
    cfg = default_config()
    return DockerTestRunner(
        tmp_path,
        cfg,
        store,
        StaticApproval("approved"),
        docker_runner=ResultDockerRunner(cfg.sandbox, docker_result),
    ).run(["python", "-m", "pytest", "-q"])


def test_install_failure_is_tests_failed_with_hint_and_summaries(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1'\n", encoding="utf-8")
    result = run_with_result(
        tmp_path,
        DockerRunResult(
            ok=False,
            exit_code=1,
            stdout="installing\n",
            stderr="HARNESS_INSTALL_FAILED\npip failed\n",
            duration_seconds=0.1,
            timed_out=False,
            image="python:3.12-slim",
            command=["python", "-m", "pytest", "-q"],
            workdir="/workspace",
        ),
    )
    assert result["status"] == "tests_failed"
    assert result["failure_hint"] == "install_failed"
    assert "installing" in result["stdout_summary"]
    assert "pip failed" in result["stderr_summary"]
    payload = (tmp_path / ".harness" / "runs" / result["run_id"] / "test_result.json").read_text(encoding="utf-8")
    assert '"failure_hint": "install_failed"' in payload


def test_missing_pytest_maps_to_pytest_missing(tmp_path) -> None:
    result = run_with_result(
        tmp_path,
        DockerRunResult(
            ok=False,
            exit_code=1,
            stdout="",
            stderr="/usr/local/bin/python: No module named pytest\n",
            duration_seconds=0.1,
            timed_out=False,
            image="python:3.12-slim",
            command=["python", "-m", "pytest", "-q"],
            workdir="/workspace",
        ),
    )
    assert result["status"] == "tests_failed"
    assert result["failure_hint"] == "pytest_missing"


def test_missing_dependency_maps_to_dependency_missing(tmp_path) -> None:
    result = run_with_result(
        tmp_path,
        DockerRunResult(
            ok=False,
            exit_code=1,
            stdout="ModuleNotFoundError: No module named 'numpy'\n",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            image="python:3.12-slim",
            command=["python", "-m", "pytest", "-q"],
            workdir="/workspace",
        ),
    )
    assert result["failure_hint"] == "dependency_missing"


def test_local_package_import_failure_maps_to_package_import_failed(tmp_path) -> None:
    result = run_with_result(
        tmp_path,
        DockerRunResult(
            ok=False,
            exit_code=1,
            stdout="ModuleNotFoundError: No module named 'harness'\n",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            image="python:3.12-slim",
            command=["python", "-m", "pytest", "-q"],
            workdir="/workspace",
        ),
    )
    assert result["failure_hint"] == "package_import_failed"


def test_pytest_failures_map_to_pytest_failures(tmp_path) -> None:
    result = run_with_result(
        tmp_path,
        DockerRunResult(
            ok=False,
            exit_code=1,
            stdout="================== FAILURES ==================\nFAILED tests/test_demo.py::test_demo\nshort test summary info\n",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            image="python:3.12-slim",
            command=["python", "-m", "pytest", "-q"],
            workdir="/workspace",
        ),
    )
    assert result["failure_hint"] == "pytest_failures"


def test_model_summary_retains_pytest_failure_context_and_bounds_output() -> None:
    long_noise = "\n".join(f"setup noise line {index}" for index in range(200))
    stdout = f"""{long_noise}
============================= FAILURES =============================
____________________________ test_important_case ____________________________

    def test_important_case():
>       assert compute() == 4
E       assert 3 == 4

tests/test_demo.py:12: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_demo.py::test_important_case - assert 3 == 4
"""

    stdout_summary, stderr_summary = summarize_test_output_for_model(stdout, "", limit=500)

    assert stderr_summary == ""
    assert len(stdout_summary) <= 520
    assert "FAILURES" in stdout_summary
    assert "test_important_case" in stdout_summary
    assert "assert 3 == 4" in stdout_summary
    assert "short test summary info" in stdout_summary


def test_model_summary_sanitizes_secret_values() -> None:
    stdout_summary, _ = summarize_test_output_for_model(
        "FAILED tests/test_secret.py::test_secret\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n",
        "",
    )

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in stdout_summary
    assert "[REDACTED_SECRET]" in stdout_summary
