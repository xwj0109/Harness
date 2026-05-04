from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.sandbox import DockerRunResult, DockerSandboxConfig


runner = CliRunner()


class FakeDockerRunner:
    created_workspaces: list[Path] = []
    run_calls: list[tuple[Path, list[str]]] = []
    preflight_calls = 0

    def __init__(self, config):
        self.config = config if isinstance(config, DockerSandboxConfig) else DockerSandboxConfig.model_validate(config)

    def preflight(self):
        FakeDockerRunner.preflight_calls += 1

    def create_workspace(self, project_root):
        from harness.sandbox import DockerSandboxRunner

        workspace = DockerSandboxRunner(DockerSandboxConfig()).create_workspace(project_root)
        FakeDockerRunner.created_workspaces.append(workspace.path)
        return workspace

    def run(self, workspace, command, timeout_seconds=None):
        FakeDockerRunner.run_calls.append((workspace.path, list(command)))
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
