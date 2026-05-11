from __future__ import annotations

import hashlib
import importlib.resources as resources
import json
from pathlib import Path
from typing import Any

from harness.execution import list_execution_adapter_descriptors
from harness.models import (
    ArtifactProvenanceRecord,
    IntegrityCheckRecord,
    IntegrityCheckResult,
    IntegrityCheckStatus,
    IntegritySubjectKind,
)
from harness.registry import builtin_spec_registry
from harness.sandbox_profiles import get_sandbox_profile
from harness.security import sanitize_for_logging
from harness.specs import HARD_FORBIDDEN_PATHS, REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS, ToolPermission


INTEGRITY_RESULT_SCHEMA_VERSION = "harness.integrity_check_result/v1"
ARTIFACT_PROVENANCE_METADATA_KEY = "provenance"
SECURITY_DOC_PATHS = ("SECURITY.md", "docs/smoke_checklist.md", "docs/command_catalog.md")
GENERATED_ARTIFACT_PRODUCERS = {
    "events": "harness.runner",
    "transcript": "harness.runner",
    "final_report": "harness.runner",
    "manifest": "harness.manifest",
    "codex_stdout": "harness.codex_isolated_edit",
    "codex_stderr": "harness.codex_isolated_edit",
    "codex_events": "harness.codex_isolated_edit",
    "codex_final_message": "harness.codex_isolated_edit",
    "repo_planning_stdout": "harness.repo_planning",
    "repo_planning_stderr": "harness.repo_planning",
    "repo_planning_events": "harness.repo_planning",
    "repo_planning_final_report": "harness.repo_planning",
}


def run_integrity_check(project_root: Path) -> IntegrityCheckResult:
    project_root = project_root.resolve()
    checks: list[IntegrityCheckRecord] = []
    checks.extend(check_builtin_spec_integrity())
    checks.extend(check_adapter_descriptor_integrity())
    checks.extend(check_security_doc_integrity(project_root))
    checks.extend(check_tui_static_asset_integrity())
    checks.sort(key=lambda item: (item.status.value, item.subject_kind.value, item.subject_id, item.id))
    summary = {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == IntegrityCheckStatus.PASS),
        "fail": sum(1 for check in checks if check.status == IntegrityCheckStatus.FAIL),
    }
    return IntegrityCheckResult(
        ok=all(check.status == IntegrityCheckStatus.PASS for check in checks),
        project_root=project_root,
        checks=checks,
        summary=summary,
    )


def check_builtin_spec_integrity() -> list[IntegrityCheckRecord]:
    checks: list[IntegrityCheckRecord] = []
    spec_files = _packaged_builtin_spec_files()
    for subject_id, item in spec_files:
        path = _traversable_path(item)
        try:
            raw = item.read_bytes()
            checks.append(
                _check(
                    IntegritySubjectKind.BUILTIN_SPEC,
                    subject_id,
                    IntegrityCheckStatus.PASS,
                    "Packaged built-in spec hash recorded.",
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    metadata={"size_bytes": len(raw)},
                )
            )
        except OSError as exc:
            checks.append(
                _check(
                    IntegritySubjectKind.BUILTIN_SPEC,
                    subject_id,
                    IntegrityCheckStatus.FAIL,
                    "Packaged built-in spec could not be read.",
                    path=path,
                    metadata={"error": str(exc)},
                )
            )
    try:
        registry = builtin_spec_registry()
        _validate_builtin_security_invariants(registry)
        checks.append(
            _check(
                IntegritySubjectKind.BUILTIN_SPEC,
                "registry_security_invariants",
                IntegrityCheckStatus.PASS,
                "Packaged built-in specs preserve local security invariants.",
                metadata={
                    "model_profiles": len(registry.model_profiles),
                    "tool_policies": len(registry.tool_policies),
                    "memory_scopes": len(registry.memory_scopes),
                    "agents": len(registry.agents),
                    "agent_profiles": len(registry.agent_profiles),
                    "workbenches": len(registry.workbenches),
                },
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                IntegritySubjectKind.BUILTIN_SPEC,
                "registry_security_invariants",
                IntegrityCheckStatus.FAIL,
                "Packaged built-in specs failed local security invariants.",
                metadata={"error": str(exc)},
            )
        )
    return checks


def check_adapter_descriptor_integrity() -> list[IntegrityCheckRecord]:
    checks: list[IntegrityCheckRecord] = []
    for descriptor in sorted(list_execution_adapter_descriptors(), key=lambda item: item.id):
        payload = descriptor.model_dump(mode="json")
        errors: list[str] = []
        if not descriptor.sandbox_profile_id:
            errors.append("missing_sandbox_profile_id")
        elif get_sandbox_profile(descriptor.sandbox_profile_id) is None:
            errors.append("unknown_sandbox_profile_id")
        if not descriptor.schema_version:
            errors.append("missing_schema_version")
        checks.append(
            _check(
                IntegritySubjectKind.ADAPTER_DESCRIPTOR,
                descriptor.id,
                IntegrityCheckStatus.FAIL if errors else IntegrityCheckStatus.PASS,
                "Registered adapter descriptor integrity recorded."
                if not errors
                else "Registered adapter descriptor failed integrity validation.",
                sha256=stable_json_sha256(payload),
                metadata={
                    "schema_version": descriptor.schema_version,
                    "sandbox_profile_id": descriptor.sandbox_profile_id,
                    "required_approvals": descriptor.required_approvals,
                    "backend_requirements": descriptor.backend_requirements,
                    "rejected_task_metadata": descriptor.rejected_task_metadata,
                    "replay_policy": descriptor.replay_policy.value,
                    "errors": errors,
                },
            )
        )
    return checks


def adapter_descriptor_evidence() -> list[dict[str, Any]]:
    evidence = []
    for descriptor in sorted(list_execution_adapter_descriptors(), key=lambda item: item.id):
        payload = descriptor.model_dump(mode="json")
        evidence.append(
            {
                "id": descriptor.id,
                "schema_version": descriptor.schema_version,
                "sandbox_profile_id": descriptor.sandbox_profile_id,
                "sha256": stable_json_sha256(payload),
            }
        )
    return evidence


def check_security_doc_integrity(project_root: Path) -> list[IntegrityCheckRecord]:
    checks: list[IntegrityCheckRecord] = []
    for relative in SECURITY_DOC_PATHS:
        path = project_root / relative
        if not path.exists():
            checks.append(
                _check(
                    IntegritySubjectKind.SECURITY_DOC,
                    relative,
                    IntegrityCheckStatus.PASS,
                    "Security-sensitive doc is not present in this package context.",
                    path=path,
                    metadata={"present": False},
                )
            )
            continue
        try:
            raw = path.read_bytes()
            checks.append(
                _check(
                    IntegritySubjectKind.SECURITY_DOC,
                    relative,
                    IntegrityCheckStatus.PASS,
                    "Security-sensitive doc hash recorded.",
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    metadata={"present": True, "size_bytes": len(raw)},
                )
            )
        except OSError as exc:
            checks.append(
                _check(
                    IntegritySubjectKind.SECURITY_DOC,
                    relative,
                    IntegrityCheckStatus.FAIL,
                    "Security-sensitive doc could not be read.",
                    path=path,
                    metadata={"present": True, "error": str(exc)},
                )
            )
    return checks


def check_tui_static_asset_integrity() -> list[IntegrityCheckRecord]:
    try:
        import harness.tui_assets.pixel_art as pixel_art

        payload = getattr(pixel_art, "TUI_PIXEL_ART_HALF_BLOCKS", [])
        return [
            _check(
                IntegritySubjectKind.TUI_STATIC_ASSET,
                "harness.tui_assets.pixel_art",
                IntegrityCheckStatus.PASS,
                "Static TUI asset hash recorded.",
                sha256=stable_json_sha256(payload),
                metadata={"rows": len(payload), "cells": sum(len(row) for row in payload if isinstance(row, list))},
            )
        ]
    except Exception as exc:
        return [
            _check(
                IntegritySubjectKind.TUI_STATIC_ASSET,
                "harness.tui_assets.pixel_art",
                IntegrityCheckStatus.FAIL,
                "Static TUI asset could not be inspected.",
                metadata={"error": str(exc)},
            )
        ]


def artifact_provenance_from_metadata(
    *,
    artifact_id: str,
    run_id: str,
    kind: str,
    producer: str | None,
    sha256: str | None,
    redaction_state: str,
    metadata: dict[str, Any],
    created_at,
) -> ArtifactProvenanceRecord:
    raw = metadata.get(ARTIFACT_PROVENANCE_METADATA_KEY)
    if isinstance(raw, dict):
        payload = {
            **raw,
            "artifact_id": raw.get("artifact_id") or artifact_id,
            "run_id": raw.get("run_id") or run_id,
            "output_sha256": raw.get("output_sha256") or sha256,
            "redaction_state": raw.get("redaction_state") or redaction_state,
            "created_at": raw.get("created_at") or created_at,
        }
        return ArtifactProvenanceRecord.model_validate(sanitize_for_logging(payload))
    inferred_producer = producer or GENERATED_ARTIFACT_PRODUCERS.get(kind)
    payload = {
        "id": artifact_provenance_id(artifact_id, kind, sha256),
        "artifact_id": artifact_id,
        "run_id": run_id,
        "producer": inferred_producer,
        "source_kind": "generated_artifact",
        "source_id": run_id,
        "input_sha256": None,
        "output_sha256": sha256,
        "redaction_state": redaction_state,
        "lineage": {
            "artifact_kind": kind,
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
        },
        "created_at": created_at,
    }
    return ArtifactProvenanceRecord.model_validate(sanitize_for_logging(payload))


def with_artifact_provenance_metadata(
    *,
    artifact_id: str,
    run_id: str,
    kind: str,
    producer: str | None,
    sha256: str | None,
    redaction_state: str,
    metadata: dict[str, Any],
    created_at,
) -> dict[str, Any]:
    clean = dict(sanitize_for_logging(metadata))
    if ARTIFACT_PROVENANCE_METADATA_KEY not in clean:
        provenance = artifact_provenance_from_metadata(
            artifact_id=artifact_id,
            run_id=run_id,
            kind=kind,
            producer=producer,
            sha256=sha256,
            redaction_state=redaction_state,
            metadata=clean,
            created_at=created_at,
        )
        clean[ARTIFACT_PROVENANCE_METADATA_KEY] = provenance.model_dump(mode="json")
    return sanitize_for_logging(clean)


def trace_export_provenance(run_id: str, trace_id: str, span_hash: str) -> dict[str, Any]:
    return sanitize_for_logging(
        {
            "schema_version": "harness.artifact_provenance/v1",
            "id": "artprov_" + hashlib.sha256(f"trace:{run_id}:{trace_id}:{span_hash}".encode("utf-8")).hexdigest()[:16],
            "run_id": run_id,
            "producer": "harness.trace_export",
            "source_kind": "trace_export",
            "source_id": trace_id,
            "output_sha256": span_hash,
            "lineage": {
                "permission_granting": False,
                "policy_authority": False,
                "approval_authority": False,
            },
        }
    )


def stable_json_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def artifact_provenance_id(artifact_id: str, kind: str, sha256: str | None) -> str:
    return "artprov_" + hashlib.sha256(f"{artifact_id}:{kind}:{sha256 or ''}".encode("utf-8")).hexdigest()[:16]


def _packaged_builtin_spec_files() -> list[tuple[str, Any]]:
    root = resources.files("harness").joinpath("builtin_specs")
    files: list[tuple[str, Any]] = []

    def visit(node, prefix: str = "") -> None:
        for child in sorted(node.iterdir(), key=lambda value: value.name):
            child_id = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir():
                visit(child, child_id)
            elif child.name.endswith(".yaml"):
                files.append((child_id, child))

    visit(root)
    return sorted(files, key=lambda item: item[0])


def _traversable_path(item) -> Path | None:
    try:
        path = Path(str(item))
    except TypeError:
        return None
    return path if path.exists() else None


def _validate_builtin_security_invariants(registry) -> None:
    for policy_id, policy in registry.tool_policies.items():
        if policy.network == ToolPermission.ALLOWED:
            raise ValueError(f"Tool policy {policy_id} allows network.")
        if policy.active_repo_write == ToolPermission.ALLOWED:
            raise ValueError(f"Tool policy {policy_id} allows active repo write.")
        if policy.hosted_boundary == ToolPermission.ALLOWED:
            raise ValueError(f"Tool policy {policy_id} allows hosted boundary.")
    for scope_id, scope in registry.memory_scopes.items():
        if not HARD_FORBIDDEN_PATHS <= set(scope.forbidden_paths):
            raise ValueError(f"Memory scope {scope_id} omits hard-forbidden paths.")
    for workbench_id, required in REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS.items():
        workbench = registry.workbenches.get(workbench_id)
        if workbench is None:
            continue
        if not required <= set(workbench.forbidden_actions):
            missing = ", ".join(sorted(required - set(workbench.forbidden_actions)))
            raise ValueError(f"Workbench {workbench_id} missing forbidden actions: {missing}")


def _check(
    subject_kind: IntegritySubjectKind,
    subject_id: str,
    status: IntegrityCheckStatus,
    message: str,
    *,
    path: Path | None = None,
    sha256: str | None = None,
    expected_sha256: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> IntegrityCheckRecord:
    clean_metadata = sanitize_for_logging(metadata or {})
    stable = {
        "subject_kind": subject_kind.value,
        "subject_id": subject_id,
        "path": str(path) if path is not None else None,
        "sha256": sha256,
        "expected_sha256": expected_sha256,
        "status": status.value,
        "metadata": clean_metadata,
    }
    return IntegrityCheckRecord(
        id="intchk_" + hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16],
        subject_kind=subject_kind,
        subject_id=subject_id,
        path=path,
        sha256=sha256,
        expected_sha256=expected_sha256,
        status=status,
        message=str(sanitize_for_logging(message)),
        metadata=clean_metadata,
    )
