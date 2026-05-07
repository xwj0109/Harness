from __future__ import annotations

from harness.models import (
    DataBoundary,
    RunMode,
    ToolCapabilityDescriptor,
    ToolReplayPolicy,
    ToolSideEffectLevel,
)


def builtin_tool_capabilities() -> dict[str, ToolCapabilityDescriptor]:
    descriptors = [
        ToolCapabilityDescriptor(
            id="repo_read",
            description="Read project files through harness path and secret-boundary guards.",
            input_schema=_object_schema({"path": "string"}),
            output_schema=_object_schema({"content": "string"}),
            side_effect_level=ToolSideEffectLevel.NONE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            replay_policy=ToolReplayPolicy.SAFE,
            allowed_run_modes=[RunMode.READ_ONLY, RunMode.PLANNING, RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT],
            policy_keys=["local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="artifact_read",
            description="Inspect registered local artifact metadata or content under harness artifact boundaries.",
            input_schema=_object_schema({"artifact_id": "string"}),
            output_schema=_object_schema({"artifact": "object"}),
            side_effect_level=ToolSideEffectLevel.NONE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            replay_policy=ToolReplayPolicy.SAFE,
            allowed_run_modes=[RunMode.READ_ONLY, RunMode.PLANNING, RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT, RunMode.TEST],
            policy_keys=["local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="artifact_write",
            description="Write or register local run artifacts under harness-managed artifact paths.",
            input_schema=_object_schema({"kind": "string", "path": "string"}),
            output_schema=_object_schema({"artifact_id": "string"}),
            side_effect_level=ToolSideEffectLevel.ARTIFACT_WRITE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            idempotency="artifact id and checksum evidence",
            replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
            allowed_run_modes=[RunMode.PLANNING, RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT, RunMode.TEST, RunMode.DEV],
            policy_keys=["local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="isolated_edit",
            description="Prepare edits inside an isolated workspace without mutating the active repository.",
            input_schema=_object_schema({"goal": "string"}),
            output_schema=_object_schema({"workspace": "string", "diff": "string"}),
            side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
            data_boundary=DataBoundary.HOSTED_PROVIDER,
            approval_required=["hosted_provider"],
            sandbox_required=True,
            idempotency="task idempotency key",
            replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
            allowed_run_modes=[RunMode.CODEX_EDIT],
            policy_keys=["hosted_boundary", "active_repo_write"],
        ),
        ToolCapabilityDescriptor(
            id="diff_inspect",
            description="Inspect isolated workspace diffs and blocked path violations.",
            input_schema=_object_schema({"workspace": "string"}),
            output_schema=_object_schema({"violations": "array", "diff_stat": "string"}),
            side_effect_level=ToolSideEffectLevel.NONE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            replay_policy=ToolReplayPolicy.SAFE,
            allowed_run_modes=[RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT],
            policy_keys=["active_repo_write", "local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="secret_scan",
            description="Scan harness-visible text for secret-like values and redact evidence.",
            input_schema=_object_schema({"text": "string"}),
            output_schema=_object_schema({"findings": "array", "redacted": "string"}),
            side_effect_level=ToolSideEffectLevel.NONE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            replay_policy=ToolReplayPolicy.SAFE,
            allowed_run_modes=[RunMode.READ_ONLY, RunMode.PLANNING, RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT, RunMode.TEST],
            policy_keys=["local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="docker_test",
            description="Run approved tests inside the configured Docker sandbox.",
            input_schema=_object_schema({"command": "array", "cwd": "string"}),
            output_schema=_object_schema({"status": "string", "artifacts": "object"}),
            side_effect_level=ToolSideEffectLevel.EXTERNAL,
            data_boundary=DataBoundary.LOCAL_ONLY,
            approval_required=["docker_execution"],
            sandbox_required=True,
            replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
            allowed_run_modes=[RunMode.TEST, RunMode.LOCAL_EDIT],
            policy_keys=["docker_execution", "local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="policy_explain",
            description="Explain runtime policy evidence for persisted harness subjects.",
            input_schema=_object_schema({"subject_kind": "string", "subject_id": "string"}),
            output_schema=_object_schema({"effective_policy": "object"}),
            side_effect_level=ToolSideEffectLevel.NONE,
            data_boundary=DataBoundary.LOCAL_ONLY,
            replay_policy=ToolReplayPolicy.SAFE,
            allowed_run_modes=[RunMode.READ_ONLY, RunMode.PLANNING, RunMode.DEV],
            policy_keys=["local_filesystem"],
        ),
        ToolCapabilityDescriptor(
            id="approval_request",
            description="Request or record explicit human approval for gated harness actions.",
            input_schema=_object_schema({"approval_kind": "string", "reason": "string"}),
            output_schema=_object_schema({"approval_id": "string", "decision": "string"}),
            side_effect_level=ToolSideEffectLevel.EXTERNAL,
            data_boundary=DataBoundary.LOCAL_ONLY,
            approval_required=["human_operator"],
            replay_policy=ToolReplayPolicy.NOT_REPLAYABLE,
            allowed_run_modes=[RunMode.PLANNING, RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT, RunMode.TEST],
            policy_keys=["hosted_boundary", "docker_execution", "active_repo_write"],
        ),
    ]
    return {descriptor.id: descriptor for descriptor in sorted(descriptors, key=lambda item: item.id)}


def list_tool_capabilities() -> list[ToolCapabilityDescriptor]:
    return list(builtin_tool_capabilities().values())


def get_tool_capability(tool_id: str) -> ToolCapabilityDescriptor:
    try:
        return builtin_tool_capabilities()[tool_id]
    except KeyError as exc:
        raise KeyError(f"Tool capability not found: {tool_id}") from exc


def _object_schema(properties: dict[str, str]) -> dict:
    return {
        "type": "object",
        "properties": {
            key: {"type": value}
            for key, value in properties.items()
        },
        "additionalProperties": False,
    }
