from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from harness.security import sanitize_for_logging


class DockerPreflightError(RuntimeError):
    pass


class DockerUnavailableError(DockerPreflightError):
    pass


class DockerImageMissingError(DockerPreflightError):
    pass


class CommandValidationError(ValueError):
    pass


class DockerSandboxConfig(BaseModel):
    image: str = "python:3.12-slim"
    network: bool = False
    timeout_seconds: int = 120
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    workdir: str = "/workspace"


class DockerRunResult(BaseModel):
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    image: str
    command: list[str]
    workdir: str


@dataclass
class SanitizedWorkspace:
    path: Path
    cleanup_status: str = "not_cleaned"

    def cleanup(self) -> None:
        if not self.path.exists():
            self.cleanup_status = "already_removed"
            return
        shutil.rmtree(self.path)
        self.cleanup_status = "cleaned"


EXCLUDED_PATTERNS = [
    ".git/",
    ".harness/",
    ".venv/",
    "node_modules/",
    "data/raw/",
    "secrets/",
    ".env",
    ".env*",
    "*.pem",
    "*.key",
    "*.sqlite",
    "*.db",
    "*.egg-info/",
    ".DS_Store",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    "dist/",
    "build/",
]

SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", "<", ">", ">>", "2>", "&>")


class DockerSandboxRunner:
    def __init__(self, config: DockerSandboxConfig) -> None:
        self.config = config
        self.docker_binary = shutil.which("docker") or "docker"

    def preflight(self) -> None:
        docker_binary = shutil.which("docker")
        self.docker_binary = docker_binary or "docker"
        try:
            version = subprocess.run(
                [self.docker_binary, "--version"],
                text=True,
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError(
                "Docker is not installed or not on PATH. Install Docker, then retry. "
                + _docker_diagnostics(docker_binary=docker_binary, error_type=type(exc).__name__)
            ) from exc
        except subprocess.SubprocessError as exc:
            raise DockerUnavailableError(
                f"Docker preflight failed: {exc}. "
                + _docker_diagnostics(docker_binary=docker_binary, error_type=type(exc).__name__)
            ) from exc
        if version.returncode != 0:
            detail = (version.stderr or version.stdout).strip()
            raise DockerUnavailableError(
                f"Docker is unavailable. Start Docker, then retry. {detail} "
                + _docker_diagnostics(
                    docker_binary=docker_binary,
                    error_type="CompletedProcess",
                    stdout=version.stdout,
                    stderr=version.stderr,
                )
            )
        image = subprocess.run(
            [self.docker_binary, "image", "inspect", self.config.image],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if image.returncode != 0:
            raise DockerImageMissingError(
                f"Docker image is missing: {self.config.image}. Run `docker pull {self.config.image}`. "
                + _docker_diagnostics(
                    docker_binary=docker_binary,
                    error_type="CompletedProcess",
                    stdout=image.stdout,
                    stderr=image.stderr,
                )
            )

    def create_workspace(self, project_root: Path) -> SanitizedWorkspace:
        source = project_root.expanduser().resolve()
        destination = Path(tempfile.mkdtemp(prefix="harness-tests-")).resolve()
        _copy_allowed_tree(source, destination)
        return SanitizedWorkspace(path=destination)

    def run(self, workspace: SanitizedWorkspace, command: list[str], timeout_seconds: int | None = None) -> DockerRunResult:
        validate_test_command(command)
        timeout = timeout_seconds or self.config.timeout_seconds
        docker_command = self.build_docker_command(workspace.path, command)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                docker_command,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            duration = time.monotonic() - started
            stdout = str(sanitize_for_logging(completed.stdout))
            stderr = str(sanitize_for_logging(completed.stderr))
            return DockerRunResult(
                ok=completed.returncode == 0,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=False,
                image=self.config.image,
                command=command,
                workdir=self.config.workdir,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            stdout = _timeout_output_to_text(exc.stdout)
            stderr = _timeout_output_to_text(exc.stderr)
            return DockerRunResult(
                ok=False,
                exit_code=None,
                stdout=str(sanitize_for_logging(stdout)),
                stderr=str(sanitize_for_logging(stderr)),
                duration_seconds=duration,
                timed_out=True,
                image=self.config.image,
                command=command,
                workdir=self.config.workdir,
            )

    def build_docker_command(self, workspace: Path, command: list[str]) -> list[str]:
        validate_test_command(command)
        args = [
            self.docker_binary,
            "run",
            "--rm",
        ]
        if not self.config.network:
            args.extend(["--network", "none"])
        args.extend(
            [
                "--memory",
                self.config.memory_limit,
                "--cpus",
                str(self.config.cpu_limit),
                "--workdir",
                self.config.workdir,
                "-v",
                f"{workspace.resolve()}:{self.config.workdir}",
                self.config.image,
            ]
        )
        args.extend(command)
        _assert_safe_docker_args(args)
        return args


def validate_test_command(command: list[str]) -> None:
    if not isinstance(command, list):
        raise CommandValidationError("Test command must be a list of strings.")
    if not command:
        raise CommandValidationError("Test command cannot be empty.")
    if len(command) == 1 and any(char.isspace() for char in command[0]):
        raise CommandValidationError("Shell-string test commands are not allowed; pass command tokens after `--`.")
    for token in command:
        if not isinstance(token, str) or token == "":
            raise CommandValidationError("Test command tokens must be non-empty strings.")
        if any(meta in token for meta in SHELL_METACHARACTERS):
            raise CommandValidationError(f"Shell metacharacters are not allowed in test command token: {token}")


def _copy_allowed_tree(source: Path, destination: Path) -> None:
    for child in source.rglob("*"):
        rel = child.relative_to(source).as_posix()
        if _is_excluded(rel):
            if child.is_dir():
                continue
            continue
        if child.is_symlink():
            if not _safe_symlink(child, source):
                continue
            continue
        target = destination / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif child.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _safe_symlink(path: Path, project_root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    root = project_root.resolve()
    return resolved == root or root in resolved.parents


def _is_excluded(relative_path: str) -> bool:
    rel = relative_path.strip("/")
    for pattern in EXCLUDED_PATTERNS:
        pat = pattern.strip()
        if pat.endswith("/"):
            prefix = pat.strip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
            if any(part == prefix or fnmatch.fnmatch(part, prefix) for part in Path(rel).parts):
                return True
        elif fnmatch.fnmatch(rel, pat) or Path(rel).match(pat):
            return True
    return False


def _assert_safe_docker_args(args: list[str]) -> None:
    if "--privileged" in args:
        raise CommandValidationError("Privileged Docker containers are not allowed.")
    for index, arg in enumerate(args[:-1]):
        if arg == "--network" and args[index + 1] == "host":
            raise CommandValidationError("Host networking is not allowed.")
        if arg in {"-v", "--volume"} and "docker.sock" in args[index + 1]:
            raise CommandValidationError("Docker socket mounts are not allowed.")
    if "/bin/sh" in args or "-c" in args:
        raise CommandValidationError("Shell execution is not allowed.")


def _timeout_output_to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _docker_diagnostics(
    docker_binary: str | None,
    error_type: str,
    stdout: str | None = None,
    stderr: str | None = None,
) -> str:
    path = os.environ.get("PATH")
    diagnostics = {
        "docker_path": docker_binary,
        "path_present": bool(path),
        "error_type": error_type,
        "stdout": stdout or "",
        "stderr": stderr or "",
    }
    return f"Diagnostics: {sanitize_for_logging(diagnostics)}"
