from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.sandbox import (
    CommandValidationError,
    DockerImageMissingError,
    DockerRunResult,
    DockerSandboxConfig,
    DockerSandboxRunner,
    DockerUnavailableError,
    validate_test_command,
)


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_docker_missing_returns_setup_guidance(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    def fake_run(args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DockerUnavailableError) as exc:
        DockerSandboxRunner(DockerSandboxConfig()).preflight()
    assert "Docker is not installed" in str(exc.value)
    assert "docker_path" in str(exc.value)
    assert "path_present" in str(exc.value)


def test_docker_image_missing_returns_pull_guidance(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/docker")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["/usr/local/bin/docker", "--version"]:
            return completed(args, stdout="Docker version")
        if args[:3] == ["/usr/local/bin/docker", "image", "inspect"]:
            return completed(args, returncode=1, stderr="missing")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DockerImageMissingError) as exc:
        DockerSandboxRunner(DockerSandboxConfig(image="python:3.12-slim")).preflight()
    assert "docker pull python:3.12-slim" in str(exc.value)
    assert not any(args[:2] == ["/usr/local/bin/docker", "run"] for args, _kwargs in calls)
    assert all("env" not in kwargs for _args, kwargs in calls)


def test_docker_preflight_uses_resolved_binary_and_inherits_cli_environment(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/opt/homebrew/bin/docker")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["/opt/homebrew/bin/docker", "--version"]:
            return completed(args, stdout="Docker version")
        if args == ["/opt/homebrew/bin/docker", "image", "inspect", "python:3.12-slim"]:
            return completed(args, stdout="[]")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerSandboxRunner(DockerSandboxConfig()).preflight()

    assert calls[0][0] == ["/opt/homebrew/bin/docker", "--version"]
    assert calls[1][0] == ["/opt/homebrew/bin/docker", "image", "inspect", "python:3.12-slim"]
    assert all("env" not in kwargs for _args, kwargs in calls)


def test_docker_command_construction_is_safe(tmp_path) -> None:
    runner = DockerSandboxRunner(DockerSandboxConfig())
    args = runner.build_docker_command(tmp_path, ["python", "-m", "pytest", "-q"])
    assert isinstance(args, list)
    assert args[1:3] == ["run", "--rm"]
    assert ["--network", "none"] == args[args.index("--network") : args.index("--network") + 2]
    assert "--memory" in args
    assert "--cpus" in args
    assert ["--workdir", "/workspace"] == args[args.index("--workdir") : args.index("--workdir") + 2]
    assert ["-v", f"{tmp_path.resolve()}:/workspace"] == args[args.index("-v") : args.index("-v") + 2]
    assert "--privileged" not in args
    assert "host" not in args
    assert "/bin/sh" not in args
    assert "-c" not in args
    assert not any("docker.sock" in arg for arg in args)
    assert "-e" not in args
    assert "--env" not in args
    assert "--env-file" not in args
    assert args[-4:] == ["python", "-m", "pytest", "-q"]


def test_install_project_false_keeps_direct_requested_command(tmp_path) -> None:
    runner = DockerSandboxRunner(DockerSandboxConfig(install_project=False))
    args = runner.build_docker_command(tmp_path, ["python", "-m", "pytest", "-q"])
    assert args[-4:] == ["python", "-m", "pytest", "-q"]
    assert not (tmp_path / ".harness_docker_run_tests.py").exists()


def test_install_project_true_generates_safe_python_runner_script(tmp_path) -> None:
    runner = DockerSandboxRunner(DockerSandboxConfig(install_project=True))
    args = runner.build_docker_command(tmp_path, ["python", "-m", "pytest", "-q"])

    assert args[-2:] == ["python", ".harness_docker_run_tests.py"]
    script = tmp_path / ".harness_docker_run_tests.py"
    text = script.read_text(encoding="utf-8")
    assert 'INSTALL_COMMAND = [sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps"]' in text
    assert 'TEST_COMMAND = ["python", "-m", "pytest", "-q"]' in text
    assert "subprocess.run(INSTALL_COMMAND, shell=False)" in text
    assert "subprocess.run(TEST_COMMAND, shell=False)" in text
    assert "/bin/sh" not in text
    assert "shell=True" not in text


@pytest.mark.parametrize("command", [["python -m pytest -q"], ["pytest", "-q;"], ["pytest", "&&", "echo"], []])
def test_command_validation_rejects_shell_strings_and_metacharacters(command) -> None:
    with pytest.raises(CommandValidationError):
        validate_test_command(command)


def test_command_validation_accepts_token_list() -> None:
    validate_test_command(["python", "-m", "pytest", "-q"])


def test_sanitized_workspace_excludes_sensitive_and_generated_paths_and_skips_symlink_escape(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("ok\n", encoding="utf-8")
    (project / ".env").write_text("SECRET=x\n", encoding="utf-8")
    (project / "secret.pem").write_text("pem\n", encoding="utf-8")
    (project / "cache.db").write_text("db\n", encoding="utf-8")
    (project / ".harness").mkdir()
    (project / ".harness" / "config.yaml").write_text("x\n", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
    (project / "agent_harness.egg-info").mkdir()
    (project / "agent_harness.egg-info" / "PKG-INFO").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (project / "escape").symlink_to(outside)
    (project / "inside_target.txt").write_text("inside\n", encoding="utf-8")
    (project / "inside_link").symlink_to(project / "inside_target.txt")

    workspace = DockerSandboxRunner(DockerSandboxConfig()).create_workspace(project)
    try:
        assert (workspace.path / "app.py").exists()
        assert not (workspace.path / ".env").exists()
        assert not (workspace.path / "secret.pem").exists()
        assert not (workspace.path / "cache.db").exists()
        assert not (workspace.path / ".harness").exists()
        assert not (workspace.path / "node_modules").exists()
        assert not (workspace.path / "agent_harness.egg-info").exists()
        assert not (workspace.path / "escape").exists()
        assert not (workspace.path / "inside_link").exists()
    finally:
        workspace.cleanup()


def test_timeout_records_partial_output(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout=1, output="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", stderr="partial")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = DockerSandboxRunner(DockerSandboxConfig()).create_workspace(tmp_path)
    try:
        result = DockerSandboxRunner(DockerSandboxConfig(timeout_seconds=1)).run(workspace, ["pytest", "-q"])
    finally:
        workspace.cleanup()
    assert result.timed_out
    assert result.exit_code is None
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.stdout
    assert "[REDACTED_SECRET]" in result.stdout


def test_exit_codes_map_to_result_ok(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        return completed(args, returncode=1, stdout="missing pytest", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = DockerSandboxRunner(DockerSandboxConfig()).create_workspace(tmp_path)
    try:
        result = DockerSandboxRunner(DockerSandboxConfig()).run(workspace, ["pytest", "-q"])
    finally:
        workspace.cleanup()
    assert isinstance(result, DockerRunResult)
    assert not result.ok
    assert result.exit_code == 1
    assert result.stdout == "missing pytest"
