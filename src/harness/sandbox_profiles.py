from __future__ import annotations

from pathlib import Path

from harness.models import (
    SandboxActiveRepoWritePolicy,
    SandboxHostFilesystemPolicy,
    SandboxNetworkPolicy,
    SandboxProfileCatalog,
    SandboxProfileDescriptor,
    SandboxTier,
)
from harness.paths import resolve_project_root


NONE_SANDBOX_PROFILE = "none"
READ_ONLY_CODEX_SANDBOX_PROFILE = "read_only_codex"
ISOLATED_WORKSPACE_CODEX_SANDBOX_PROFILE = "isolated_workspace_codex"
DOCKER_TEST_SANDBOX_PROFILE = "docker_test_sandbox"


def builtin_sandbox_profiles() -> dict[str, SandboxProfileDescriptor]:
    profiles = [
        SandboxProfileDescriptor(
            id=NONE_SANDBOX_PROFILE,
            tier=SandboxTier.NONE,
            network=SandboxNetworkPolicy.FORBIDDEN,
            active_repo_write=SandboxActiveRepoWritePolicy.FORBIDDEN,
            host_filesystem=SandboxHostFilesystemPolicy.FORBIDDEN,
            resource_limits={},
            forbidden_mounts=[],
            secret_path_policy="no secret or project-private path access",
            notes=["Evidence-only path; does not invoke tools, backends, Docker, shell, network, hosted providers, or paid providers."],
        ),
        SandboxProfileDescriptor(
            id=READ_ONLY_CODEX_SANDBOX_PROFILE,
            tier=SandboxTier.READ_ONLY,
            network=SandboxNetworkPolicy.FORBIDDEN,
            active_repo_write=SandboxActiveRepoWritePolicy.FORBIDDEN,
            host_filesystem=SandboxHostFilesystemPolicy.FORBIDDEN,
            resource_limits={},
            forbidden_mounts=[],
            secret_path_policy="context is filtered by Harness path and secret-boundary guards before hosted-boundary execution",
            notes=["Requires Codex CLI read-only sandbox support and hosted-boundary approval before run creation."],
        ),
        SandboxProfileDescriptor(
            id=ISOLATED_WORKSPACE_CODEX_SANDBOX_PROFILE,
            tier=SandboxTier.ISOLATED_WORKSPACE,
            network=SandboxNetworkPolicy.FORBIDDEN,
            active_repo_write=SandboxActiveRepoWritePolicy.APPROVAL_REQUIRED,
            host_filesystem=SandboxHostFilesystemPolicy.ISOLATED_WORKSPACE,
            resource_limits={},
            forbidden_mounts=[],
            secret_path_policy="isolated workspace is created from filtered project context; apply-back rejects secret-like paths",
            notes=["Active repository mutation remains denied until separate inspected apply-back approval."],
        ),
        SandboxProfileDescriptor(
            id=DOCKER_TEST_SANDBOX_PROFILE,
            tier=SandboxTier.DOCKER_SANDBOX,
            network=SandboxNetworkPolicy.FORBIDDEN,
            active_repo_write=SandboxActiveRepoWritePolicy.FORBIDDEN,
            host_filesystem=SandboxHostFilesystemPolicy.SANITIZED_COPY,
            resource_limits={"memory": "2g", "cpus": 2.0, "timeout_seconds": 120},
            forbidden_mounts=["/var/run/docker.sock", "host network", "active project root"],
            secret_path_policy="sanitized workspace excludes .harness, .git, .env*, *.pem, *.key, *.sqlite, *.db, and secrets/",
            notes=["Direct Docker test execution only; network is disabled by default and command tokens reject shell metacharacters."],
        ),
    ]
    return {profile.id: profile for profile in profiles}


def list_sandbox_profiles() -> list[SandboxProfileDescriptor]:
    return list(builtin_sandbox_profiles().values())


def get_sandbox_profile(profile_id: str) -> SandboxProfileDescriptor:
    try:
        return builtin_sandbox_profiles()[profile_id]
    except KeyError as exc:
        raise KeyError(f"Sandbox profile not found: {profile_id}") from exc


def build_sandbox_profile_catalog(project_root: Path) -> SandboxProfileCatalog:
    return SandboxProfileCatalog(project_root=resolve_project_root(project_root), profiles=list_sandbox_profiles())


def sandbox_profile_dict(profile_id: str | None) -> dict | None:
    if profile_id is None:
        return None
    return get_sandbox_profile(profile_id).model_dump(mode="json")
