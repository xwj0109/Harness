from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from harness.config import HarnessConfig
from harness.paths import PathSecurityError, resolve_under_project
from harness.security import sanitize_for_logging


MANAGED_TEST_DOCKERFILE = """FROM python:3.12-slim

WORKDIR /workspace

RUN apt-get update \\
    && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \\
    "setuptools>=68" \\
    wheel \\
    pytest \\
    typer \\
    pydantic \\
    pyyaml

ENV PYTHONPATH=/workspace

CMD ["python", "-m", "pytest", "-q"]
"""


class DockerfileValidationResult(BaseModel):
    ok: bool
    dockerfile: str
    image: str
    issues: list[str]
    warnings: list[str]


class DockerImageBuildResult(BaseModel):
    ok: bool
    image: str
    dockerfile: str
    command: list[str]
    stdout: str
    stderr: str
    guidance: str


class DockerImageManager:
    def __init__(self, project_root: Path, config: HarnessConfig) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.image = config.sandbox.image

    def dockerfile_path(self) -> Path:
        configured = self.config.sandbox.image_build_file
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("sandbox.image_build_file must be a non-empty project-relative path.")
        raw = Path(configured)
        if raw.is_absolute():
            raise ValueError("sandbox.image_build_file must be project-relative.")
        try:
            return resolve_under_project(self.project_root, raw)
        except PathSecurityError as exc:
            raise ValueError(str(exc)) from exc

    def validate_dockerfile(self) -> DockerfileValidationResult:
        path = self.dockerfile_path()
        issues: list[str] = []
        warnings: list[str] = []
        if not path.exists():
            issues.append(
                f"Dockerfile not found. Generate one with `harness tests image generate --project {self.project_root}` "
                f"or set sandbox.image_build_file."
            )
            return DockerfileValidationResult(
                ok=False,
                dockerfile=str(path),
                image=self.image,
                issues=issues,
                warnings=warnings,
            )
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        if "from " not in lowered:
            issues.append("Dockerfile must contain a FROM instruction.")
        if "python:" not in lowered:
            warnings.append("Managed harness test images usually start from a Python base image.")
        if "pytest" not in lowered:
            issues.append("Dockerfile should install pytest so `python -m pytest -q` can run.")
        if "git" not in lowered:
            warnings.append("Git is recommended because the harness test suite creates temporary Git repositories.")
        if "--no-install-recommends" not in lowered and "apt-get install" in lowered:
            warnings.append("Use apt-get install --no-install-recommends to keep the image small.")
        if "rm -rf /var/lib/apt/lists" not in lowered and "apt-get update" in lowered:
            warnings.append("Clean /var/lib/apt/lists/* after apt installs.")
        if "docker.sock" in lowered:
            issues.append("Docker socket references are not allowed in the managed test image Dockerfile.")
        if "--network host" in lowered or "network=host" in lowered:
            issues.append("Host networking must not be configured in the managed test image Dockerfile.")
        if ".env" in lowered or "secrets/" in lowered:
            issues.append("Dockerfile must not copy or reference .env files or secrets/.")
        return DockerfileValidationResult(
            ok=not issues,
            dockerfile=str(path),
            image=self.image,
            issues=issues,
            warnings=warnings,
        )

    def generate_dockerfile(self, force: bool = False) -> Path:
        path = self.dockerfile_path()
        if path.exists() and not force:
            raise FileExistsError(f"Dockerfile already exists: {path}. Pass --force to overwrite it.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MANAGED_TEST_DOCKERFILE, encoding="utf-8")
        return path

    def build_image(self) -> DockerImageBuildResult:
        validation = self.validate_dockerfile()
        command = ["docker", "build", "-f", validation.dockerfile, "-t", self.image, str(self.project_root)]
        if not validation.ok:
            return DockerImageBuildResult(
                ok=False,
                image=self.image,
                dockerfile=validation.dockerfile,
                command=command,
                stdout="",
                stderr="\n".join(validation.issues),
                guidance="Fix the Dockerfile validation issues before building.",
            )
        docker_binary = shutil.which("docker")
        if docker_binary is None:
            return DockerImageBuildResult(
                ok=False,
                image=self.image,
                dockerfile=validation.dockerfile,
                command=command,
                stdout="",
                stderr="Docker is not installed or not on PATH.",
                guidance="Install/start Docker and verify `docker --version` works in this terminal.",
            )
        command[0] = docker_binary
        try:
            version = subprocess.run([docker_binary, "--version"], text=True, capture_output=True, timeout=15)
            if version.returncode != 0:
                return DockerImageBuildResult(
                    ok=False,
                    image=self.image,
                    dockerfile=validation.dockerfile,
                    command=command,
                    stdout=str(sanitize_for_logging(version.stdout)),
                    stderr=str(sanitize_for_logging(version.stderr)),
                    guidance="Start Docker and verify `docker --version` succeeds.",
                )
            completed = subprocess.run(command, text=True, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            return DockerImageBuildResult(
                ok=False,
                image=self.image,
                dockerfile=validation.dockerfile,
                command=command,
                stdout="",
                stderr=str(sanitize_for_logging(str(exc))),
                guidance="Docker image build failed before completion.",
            )
        return DockerImageBuildResult(
            ok=completed.returncode == 0,
            image=self.image,
            dockerfile=validation.dockerfile,
            command=command,
            stdout=str(sanitize_for_logging(completed.stdout)),
            stderr=str(sanitize_for_logging(completed.stderr)),
            guidance="" if completed.returncode == 0 else "Inspect Docker build output and update the configured Dockerfile.",
        )
