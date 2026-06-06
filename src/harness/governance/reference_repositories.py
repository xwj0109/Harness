from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = "harness.reference_repositories_audit/v1"


@dataclass(frozen=True)
class ReferenceRepositoryProfile:
    name: str
    upstream: str
    reference_patterns: tuple[str, ...]
    integration_role: str
    implementation_guidance: tuple[str, ...]


CURATED_REFERENCE_REPOSITORIES: tuple[str, ...] = (
    "A2A",
    "bubblewrap",
    "containerd",
    "dapr-agents",
    "firecracker",
    "google-adk-python",
    "gvisor",
    "kata-containers",
    "langgraph",
    "microsoft-agent-framework",
    "modelcontextprotocol",
    "nsjail",
    "openai-agents-js",
    "openai-agents-python",
    "opentelemetry-semantic-conventions",
    "runc",
    "temporal-sdk-python",
)

REQUIRED_REFERENCE_PATTERNS: tuple[str, ...] = (
    "agent_runtime",
    "durable_workflow",
    "external_protocol",
    "low_level_isolation",
    "observability",
    "policy_boundary",
    "sandbox_runtime",
    "state_graph",
    "tool_contracts",
)

REFERENCE_REPOSITORY_PROFILES: dict[str, ReferenceRepositoryProfile] = {
    "A2A": ReferenceRepositoryProfile(
        name="A2A",
        upstream="a2aproject/A2A",
        reference_patterns=("external_protocol", "agent_handoff", "tool_contracts"),
        integration_role="Cross-agent protocol reference for typed handoff envelopes.",
        implementation_guidance=(
            "Translate protocol envelope and discovery ideas into Harness contracts.",
            "Do not import protocol server code without a separate license and threat-model review.",
        ),
    ),
    "bubblewrap": ReferenceRepositoryProfile(
        name="bubblewrap",
        upstream="containers/bubblewrap",
        reference_patterns=("low_level_isolation", "sandbox_runtime", "filesystem_boundary"),
        integration_role="Linux user-namespace isolation reference for local process boundaries.",
        implementation_guidance=(
            "Use as a boundary model for filesystem and process isolation checks.",
            "Keep Harness sandbox policy declarative; do not shell out to reference binaries implicitly.",
        ),
    ),
    "containerd": ReferenceRepositoryProfile(
        name="containerd",
        upstream="containerd/containerd",
        reference_patterns=("sandbox_runtime", "runtime_supervision", "artifact_lifecycle"),
        integration_role="Container runtime lifecycle reference for execution supervision.",
        implementation_guidance=(
            "Mirror lifecycle state-machine discipline for adapters and breakers.",
            "Avoid vendoring runtime code into Harness orchestration paths.",
        ),
    ),
    "dapr-agents": ReferenceRepositoryProfile(
        name="dapr-agents",
        upstream="dapr/dapr-agents",
        reference_patterns=("agent_runtime", "durable_workflow", "distributed_coordination", "policy_boundary"),
        integration_role="Actor/service orchestration reference for distributed agent coordination.",
        implementation_guidance=(
            "Translate actor and pub/sub separation into Harness dispatcher contracts.",
            "Keep networked sidecar patterns behind explicit configuration and approval boundaries.",
        ),
    ),
    "firecracker": ReferenceRepositoryProfile(
        name="firecracker",
        upstream="firecracker-microvm/firecracker",
        reference_patterns=("low_level_isolation", "sandbox_runtime", "security_boundary"),
        integration_role="MicroVM isolation reference for high-risk execution boundaries.",
        implementation_guidance=(
            "Use as a benchmark for explicit device, filesystem, and network deny-by-default policy.",
            "Do not imply Firecracker execution support from reference availability.",
        ),
    ),
    "google-adk-python": ReferenceRepositoryProfile(
        name="google-adk-python",
        upstream="google/adk-python",
        reference_patterns=("agent_runtime", "tool_contracts", "policy_boundary"),
        integration_role="Agent/tool declaration reference for typed tool execution surfaces.",
        implementation_guidance=(
            "Compare tool schema and callback boundaries against Harness session-tool descriptors.",
            "Keep provider-specific behavior behind adapters.",
        ),
    ),
    "gvisor": ReferenceRepositoryProfile(
        name="gvisor",
        upstream="google/gvisor",
        reference_patterns=("low_level_isolation", "sandbox_runtime", "security_boundary"),
        integration_role="User-space kernel isolation reference for syscall boundary thinking.",
        implementation_guidance=(
            "Use as a threat-model reference for process execution and filesystem mediation.",
            "Do not treat local checkout presence as runtime availability.",
        ),
    ),
    "kata-containers": ReferenceRepositoryProfile(
        name="kata-containers",
        upstream="kata-containers/kata-containers",
        reference_patterns=("low_level_isolation", "sandbox_runtime", "security_boundary"),
        integration_role="VM-backed container isolation reference for hardened adapter execution.",
        implementation_guidance=(
            "Translate isolation profiles into Harness sandbox metadata and readiness gates.",
            "Do not import runtime integration code without a separate operator decision.",
        ),
    ),
    "langgraph": ReferenceRepositoryProfile(
        name="langgraph",
        upstream="langchain-ai/langgraph",
        reference_patterns=("state_graph", "durable_workflow", "checkpointing", "agent_runtime"),
        integration_role="State graph and checkpoint reference for resumable orchestration.",
        implementation_guidance=(
            "Use graph/checkpoint semantics to evaluate Harness objective and task evidence.",
            "Prefer Harness append-only evidence over opaque in-memory graph state.",
        ),
    ),
    "microsoft-agent-framework": ReferenceRepositoryProfile(
        name="microsoft-agent-framework",
        upstream="microsoft/agent-framework",
        reference_patterns=("agent_runtime", "tool_contracts", "policy_boundary", "progress_observability"),
        integration_role="Multi-agent runtime reference for typed agents, tools, progress, and approvals.",
        implementation_guidance=(
            "Translate explicit agent/tool contracts into Harness descriptors and readiness checks.",
            "Keep Harness permission records as the authority source instead of adopting ambient framework defaults.",
        ),
    ),
    "modelcontextprotocol": ReferenceRepositoryProfile(
        name="modelcontextprotocol",
        upstream="modelcontextprotocol/modelcontextprotocol",
        reference_patterns=("external_protocol", "tool_contracts", "resource_boundary", "policy_boundary"),
        integration_role="Tool/resource protocol reference for extension boundary contracts.",
        implementation_guidance=(
            "Map server/resource exposure into Harness MCP and session-tool policy projections.",
            "Require exact origin, scope, and permission metadata before body injection.",
        ),
    ),
    "nsjail": ReferenceRepositoryProfile(
        name="nsjail",
        upstream="google/nsjail",
        reference_patterns=("low_level_isolation", "sandbox_runtime", "security_boundary"),
        integration_role="Process sandbox reference for namespace, seccomp, and resource-limit controls.",
        implementation_guidance=(
            "Use as a checklist for execution adapter sandbox metadata.",
            "Do not execute reference jail tooling from passive readiness checks.",
        ),
    ),
    "openai-agents-js": ReferenceRepositoryProfile(
        name="openai-agents-js",
        upstream="openai/openai-agents-js",
        reference_patterns=("agent_runtime", "tool_contracts", "handoff_contracts", "policy_boundary"),
        integration_role="Agent handoff and tool contract reference for JavaScript runtimes.",
        implementation_guidance=(
            "Compare handoff and tool schema boundaries against Harness native tool exposure.",
            "Avoid runtime coupling to SDK internals.",
        ),
    ),
    "openai-agents-python": ReferenceRepositoryProfile(
        name="openai-agents-python",
        upstream="openai/openai-agents-python",
        reference_patterns=("agent_runtime", "tool_contracts", "handoff_contracts", "policy_boundary"),
        integration_role="Agent handoff and tool contract reference for Python runtimes.",
        implementation_guidance=(
            "Use as a reference for model/tool loop event boundaries and recovery behavior.",
            "Keep provider invocation behind Harness provider adapters and approvals.",
        ),
    ),
    "opentelemetry-semantic-conventions": ReferenceRepositoryProfile(
        name="opentelemetry-semantic-conventions",
        upstream="open-telemetry/semantic-conventions",
        reference_patterns=("observability", "trace_semantics", "event_metadata"),
        integration_role="Trace/event semantic reference for run and objective observability.",
        implementation_guidance=(
            "Map Harness run, objective, tool, and adapter events to stable span metadata.",
            "Never include raw artifact or transcript bodies in passive trace projections.",
        ),
    ),
    "runc": ReferenceRepositoryProfile(
        name="runc",
        upstream="opencontainers/runc",
        reference_patterns=("sandbox_runtime", "low_level_isolation", "oci_runtime"),
        integration_role="OCI runtime reference for container execution boundaries.",
        implementation_guidance=(
            "Use OCI lifecycle concepts to harden adapter execution contracts.",
            "Do not assume host runc availability from reference checkout metadata.",
        ),
    ),
    "temporal-sdk-python": ReferenceRepositoryProfile(
        name="temporal-sdk-python",
        upstream="temporalio/sdk-python",
        reference_patterns=("durable_workflow", "checkpointing", "retry_policy", "observability"),
        integration_role="Durable workflow reference for retries, history, and deterministic orchestration.",
        implementation_guidance=(
            "Translate durable history and retry discipline into Harness objective evidence.",
            "Keep workflow replay as metadata/evidence verification unless an adapter explicitly executes.",
        ),
    ),
}

GitRunner = Callable[[Path, list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ReferenceRepositoryRecord:
    name: str
    path: str
    git_present: bool
    head_sha: str | None
    current_branch: str | None
    remote_origin_url: str | None
    dirty: bool
    dirty_count: int
    lfs_available: bool
    lfs_used: bool
    lfs_file_count: int
    lfs_materialized_file_count: int
    lfs_unmaterialized_file_count: int
    curated_expected: bool
    profile_present: bool = False
    upstream: str | None = None
    reference_patterns: tuple[str, ...] = ()
    integration_role: str | None = None
    implementation_guidance: tuple[str, ...] = ()
    license_review_required: bool = True
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    manual_review_required: bool = True
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["warnings"] = list(self.warnings)
        payload["errors"] = list(self.errors)
        payload["reference_patterns"] = list(self.reference_patterns)
        payload["implementation_guidance"] = list(self.implementation_guidance)
        return payload


@dataclass(frozen=True)
class ReferenceRepositoriesAudit:
    generated_at: str
    project_root: str
    reference_root: str
    root_exists: bool
    root_is_directory: bool
    expected_repository_names: tuple[str, ...]
    required_reference_patterns: tuple[str, ...]
    covered_reference_patterns: tuple[str, ...]
    missing_required_reference_patterns: tuple[str, ...]
    reference_pattern_coverage: dict[str, tuple[str, ...]]
    missing_expected_repository_names: tuple[str, ...]
    extra_repository_names: tuple[str, ...]
    repositories: tuple[ReferenceRepositoryRecord, ...]
    summary: dict[str, Any]
    warnings: tuple[str, ...] = ()
    authority: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": self.root_is_directory or not self.root_exists,
            "generated_at": self.generated_at,
            "project_root": self.project_root,
            "reference_root": self.reference_root,
            "root_exists": self.root_exists,
            "root_is_directory": self.root_is_directory,
            "expected_repository_names": list(self.expected_repository_names),
            "required_reference_patterns": list(self.required_reference_patterns),
            "covered_reference_patterns": list(self.covered_reference_patterns),
            "missing_required_reference_patterns": list(self.missing_required_reference_patterns),
            "reference_pattern_coverage": {
                pattern: list(names) for pattern, names in sorted(self.reference_pattern_coverage.items())
            },
            "missing_expected_repository_names": list(self.missing_expected_repository_names),
            "extra_repository_names": list(self.extra_repository_names),
            "authority": self.authority or _reference_authority(),
            "summary": self.summary,
            "warnings": list(self.warnings),
            "repositories": [repo.to_dict() for repo in self.repositories],
        }


def build_reference_repositories_audit(
    project_root: Path,
    *,
    reference_root: Path | None = None,
    now: datetime | None = None,
    runner: GitRunner | None = None,
    expected_repository_names: Iterable[str] | None = CURATED_REFERENCE_REPOSITORIES,
    required_reference_patterns: Iterable[str] | None = REQUIRED_REFERENCE_PATTERNS,
) -> ReferenceRepositoriesAudit:
    root = Path(project_root).expanduser().resolve()
    refs_root = (
        _default_reference_root(root)
        if reference_root is None
        else Path(reference_root).expanduser().resolve()
    )
    generated_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")
    expected_names = tuple(sorted({str(name) for name in (expected_repository_names or ()) if str(name).strip()}))
    expected_name_set = set(expected_names)
    required_patterns = tuple(
        sorted({str(pattern) for pattern in (required_reference_patterns or ()) if str(pattern).strip()})
    )
    warnings: list[str] = []
    repositories: list[ReferenceRepositoryRecord] = []
    missing_git_count = 0

    root_exists = refs_root.exists()
    root_is_directory = refs_root.is_dir()
    if not root_exists:
        warnings.append("reference_root_missing")
    elif not root_is_directory:
        warnings.append("reference_root_not_directory")
    else:
        candidates = _candidate_repository_dirs(refs_root)
        if refs_root not in candidates:
            missing_git_count = sum(
                1 for child in refs_root.iterdir() if child.is_dir() and not (child / ".git").exists()
            )
        for candidate in candidates:
            repositories.append(_audit_repository(candidate, runner=runner or _run_git, expected_names=expected_name_set))

    repositories_tuple = tuple(sorted(repositories, key=lambda repo: repo.name))
    repository_names = {repo.name for repo in repositories_tuple}
    missing_expected_names = tuple(sorted(expected_name_set - repository_names))
    extra_repository_names = tuple(sorted(repository_names - expected_name_set)) if expected_name_set else ()
    reference_pattern_coverage = _reference_pattern_coverage(repositories_tuple)
    covered_reference_patterns = tuple(sorted(reference_pattern_coverage))
    missing_required_patterns = tuple(sorted(set(required_patterns) - set(covered_reference_patterns)))
    if missing_expected_names:
        warnings.append("expected_repositories_missing")
    if missing_required_patterns:
        warnings.append("required_reference_patterns_missing")
    if any(repo.lfs_unmaterialized_file_count > 0 for repo in repositories_tuple):
        warnings.append("git_lfs_files_unmaterialized")
    summary = _summarize(
        repositories_tuple,
        missing_git_count=missing_git_count,
        root_exists=root_exists,
        root_is_directory=root_is_directory,
        expected_repository_names=expected_names,
        required_reference_patterns=required_patterns,
        covered_reference_patterns=covered_reference_patterns,
        missing_required_reference_patterns=missing_required_patterns,
        missing_expected_repository_names=missing_expected_names,
        extra_repository_names=extra_repository_names,
    )
    return ReferenceRepositoriesAudit(
        generated_at=generated_at,
        project_root=str(root),
        reference_root=str(refs_root),
        root_exists=root_exists,
        root_is_directory=root_is_directory,
        expected_repository_names=expected_names,
        required_reference_patterns=required_patterns,
        covered_reference_patterns=covered_reference_patterns,
        missing_required_reference_patterns=missing_required_patterns,
        reference_pattern_coverage=reference_pattern_coverage,
        missing_expected_repository_names=missing_expected_names,
        extra_repository_names=extra_repository_names,
        repositories=repositories_tuple,
        summary=summary,
        warnings=tuple(_dedupe(warnings)),
        authority=_reference_authority(),
    )


def _default_reference_root(project_root: Path) -> Path:
    return project_root.with_name(f"{project_root.name}-references")


def _candidate_repository_dirs(reference_root: Path) -> tuple[Path, ...]:
    if (reference_root / ".git").exists():
        return (reference_root,)
    return tuple(
        sorted(
            (child for child in reference_root.iterdir() if child.is_dir() and (child / ".git").exists()),
            key=lambda path: path.name,
        )
    )


def _audit_repository(repo: Path, *, runner: GitRunner, expected_names: set[str]) -> ReferenceRepositoryRecord:
    warnings: list[str] = []
    errors: list[str] = []
    profile = REFERENCE_REPOSITORY_PROFILES.get(repo.name)

    head_sha = _git_output(repo, ["rev-parse", "--verify", "HEAD"], runner=runner)
    if head_sha is None:
        warnings.append("head_unavailable")

    branch = _git_output(repo, ["branch", "--show-current"], runner=runner)
    if branch == "":
        branch = None

    remote = _git_output(repo, ["remote", "get-url", "origin"], runner=runner)
    if remote:
        remote = _redact_remote_url(remote)

    status = _git_result(repo, ["status", "--porcelain=v1", "--untracked-files=normal"], runner=runner)
    if status.returncode == 0:
        dirty_count = len([line for line in status.stdout.splitlines() if line.strip()])
    else:
        dirty_count = 0
        errors.append("git_status_failed")

    lfs_available = _git_result(repo, ["lfs", "version"], runner=runner).returncode == 0
    lfs_file_count = 0
    lfs_materialized_file_count = 0
    lfs_unmaterialized_file_count = 0
    if lfs_available:
        lfs_result = _git_result(repo, ["lfs", "ls-files"], runner=runner)
        if lfs_result.returncode == 0:
            lfs_records = [_parse_lfs_ls_files_line(line) for line in lfs_result.stdout.splitlines() if line.strip()]
            lfs_file_count = len(lfs_records)
            lfs_materialized_file_count = sum(1 for record in lfs_records if record == "materialized")
            lfs_unmaterialized_file_count = sum(1 for record in lfs_records if record == "unmaterialized")
            if lfs_unmaterialized_file_count:
                warnings.append("git_lfs_files_unmaterialized")
        else:
            warnings.append("git_lfs_ls_files_failed")
    else:
        warnings.append("git_lfs_unavailable")

    return ReferenceRepositoryRecord(
        name=repo.name,
        path=str(repo.resolve()),
        git_present=True,
        head_sha=head_sha,
        current_branch=branch,
        remote_origin_url=remote,
        dirty=dirty_count > 0,
        dirty_count=dirty_count,
        lfs_available=lfs_available,
        lfs_used=lfs_file_count > 0,
        lfs_file_count=lfs_file_count,
        lfs_materialized_file_count=lfs_materialized_file_count,
        lfs_unmaterialized_file_count=lfs_unmaterialized_file_count,
        curated_expected=repo.name in expected_names,
        profile_present=profile is not None,
        upstream=profile.upstream if profile is not None else None,
        reference_patterns=profile.reference_patterns if profile is not None else (),
        integration_role=profile.integration_role if profile is not None else None,
        implementation_guidance=profile.implementation_guidance if profile is not None else (),
        warnings=tuple(_dedupe(warnings)),
        errors=tuple(_dedupe(errors)),
    )


def _parse_lfs_ls_files_line(line: str) -> str:
    parts = line.split()
    if len(parts) >= 3 and parts[1] == "*":
        return "materialized"
    if len(parts) >= 3 and parts[1] == "-":
        return "unmaterialized"
    return "unknown"


def _git_output(repo: Path, args: list[str], *, runner: GitRunner) -> str | None:
    result = _git_result(repo, args, runner=runner)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_result(repo: Path, args: list[str], *, runner: GitRunner) -> subprocess.CompletedProcess[str]:
    return runner(repo, args)


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(repo), *args],
            returncode=1,
            stdout="",
            stderr=type(exc).__name__,
        )


def _redact_remote_url(remote: str) -> str:
    value = remote.strip()
    if "://" not in value:
        return value
    parsed = urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def _summarize(
    repositories: tuple[ReferenceRepositoryRecord, ...],
    *,
    missing_git_count: int,
    root_exists: bool,
    root_is_directory: bool,
    expected_repository_names: tuple[str, ...],
    required_reference_patterns: tuple[str, ...],
    covered_reference_patterns: tuple[str, ...],
    missing_required_reference_patterns: tuple[str, ...],
    missing_expected_repository_names: tuple[str, ...],
    extra_repository_names: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "root_exists": root_exists,
        "root_is_directory": root_is_directory,
        "expected_repository_count": len(expected_repository_names),
        "missing_expected_repository_count": len(missing_expected_repository_names),
        "extra_repository_count": len(extra_repository_names),
        "repository_count": len(repositories),
        "profiled_repository_count": sum(1 for repo in repositories if repo.profile_present),
        "unprofiled_repository_count": sum(1 for repo in repositories if not repo.profile_present),
        "required_reference_pattern_count": len(required_reference_patterns),
        "covered_reference_pattern_count": len(covered_reference_patterns),
        "missing_required_reference_pattern_count": len(missing_required_reference_patterns),
        "missing_required_reference_patterns": list(missing_required_reference_patterns),
        "dirty_repository_count": sum(1 for repo in repositories if repo.dirty),
        "dirty_file_count": sum(repo.dirty_count for repo in repositories),
        "lfs_repository_count": sum(1 for repo in repositories if repo.lfs_used),
        "lfs_file_count": sum(repo.lfs_file_count for repo in repositories),
        "lfs_materialized_file_count": sum(repo.lfs_materialized_file_count for repo in repositories),
        "lfs_unmaterialized_file_count": sum(repo.lfs_unmaterialized_file_count for repo in repositories),
        "missing_git_count": missing_git_count,
        "manual_review_required_count": len(repositories),
        "contents_included": False,
        "execution_allowed": False,
        "model_context_allowed": False,
        "network_required": False,
        "mutation_allowed": False,
    }


def _reference_pattern_coverage(
    repositories: tuple[ReferenceRepositoryRecord, ...],
) -> dict[str, tuple[str, ...]]:
    coverage: dict[str, list[str]] = {}
    for repo in repositories:
        for pattern in repo.reference_patterns:
            coverage.setdefault(pattern, []).append(repo.name)
    return {pattern: tuple(sorted(names)) for pattern, names in coverage.items()}


def _reference_authority() -> dict[str, bool]:
    return {
        "read_only": True,
        "contents_included": False,
        "execution_allowed": False,
        "model_context_allowed": False,
        "network_required": False,
        "mutation_allowed": False,
        "permission_granting": False,
        "manual_review_required": True,
    }


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
