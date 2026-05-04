from __future__ import annotations

import subprocess

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config, write_default_config
from harness.sandbox import DockerImageManager


runner = CliRunner()


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_generate_and_validate_managed_dockerfile(tmp_path) -> None:
    cfg = default_config()
    manager = DockerImageManager(tmp_path, cfg)

    path = manager.generate_dockerfile()
    result = manager.validate_dockerfile()

    assert path == tmp_path / "Dockerfile.harness-test"
    assert result.ok
    assert result.issues == []
    assert "pytest" in path.read_text(encoding="utf-8")
    assert "git" in path.read_text(encoding="utf-8")


def test_validate_missing_dockerfile_returns_generation_guidance(tmp_path) -> None:
    result = DockerImageManager(tmp_path, default_config()).validate_dockerfile()

    assert not result.ok
    assert "harness tests image generate" in result.issues[0]


def test_validate_rejects_dockerfile_referencing_secrets(tmp_path) -> None:
    (tmp_path / "Dockerfile.harness-test").write_text(
        "FROM python:3.12-slim\nCOPY .env /workspace/.env\nRUN pip install pytest\n",
        encoding="utf-8",
    )

    result = DockerImageManager(tmp_path, default_config()).validate_dockerfile()

    assert not result.ok
    assert any(".env" in issue for issue in result.issues)


def test_image_build_uses_docker_build_argument_list(monkeypatch, tmp_path) -> None:
    DockerImageManager(tmp_path, default_config()).generate_dockerfile()
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/docker")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["/usr/local/bin/docker", "--version"]:
            return completed(args, stdout="Docker version")
        if args[:2] == ["/usr/local/bin/docker", "build"]:
            return completed(args, stdout="built")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerImageManager(tmp_path, default_config()).build_image()

    assert result.ok
    assert calls[1][0] == [
        "/usr/local/bin/docker",
        "build",
        "-f",
        str(tmp_path / "Dockerfile.harness-test"),
        "-t",
        "python:3.12-slim",
        str(tmp_path.resolve()),
    ]
    assert all("env" not in kwargs for _args, kwargs in calls)
    build_args = calls[1][0]
    assert "/bin/sh" not in build_args
    assert "-c" not in build_args
    assert "--network" not in build_args
    assert "--privileged" not in build_args


def test_image_build_missing_docker_fails_without_run_or_fallback(monkeypatch, tmp_path) -> None:
    DockerImageManager(tmp_path, default_config()).generate_dockerfile()
    monkeypatch.setattr("shutil.which", lambda name: None)

    def forbidden_run(*args, **kwargs):
        raise AssertionError("Docker subprocess should not run when docker is not on PATH.")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    result = DockerImageManager(tmp_path, default_config()).build_image()

    assert not result.ok
    assert "Docker is not installed" in result.stderr


def test_cli_tests_image_generate_validate_and_build(monkeypatch, tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    generated = runner.invoke(app, ["tests", "image", "generate", "--project", str(tmp_path)])
    assert generated.exit_code == 0
    assert (tmp_path / "Dockerfile.harness-test").exists()

    validated = runner.invoke(app, ["tests", "image", "validate", "--project", str(tmp_path)])
    assert validated.exit_code == 0
    assert "Valid: True" in validated.output

    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/docker")

    def fake_run(args, **kwargs):
        if args == ["/usr/local/bin/docker", "--version"]:
            return completed(args, stdout="Docker version")
        if args[:2] == ["/usr/local/bin/docker", "build"]:
            return completed(args, stdout="built")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    built = runner.invoke(app, ["tests", "image", "build", "--project", str(tmp_path)])
    assert built.exit_code == 0
    assert "Built: True" in built.output
    assert "docker build" in built.output
