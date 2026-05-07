from __future__ import annotations

import json
from pathlib import Path

from harness.config import HarnessConfig
from harness.memory.sqlite_store import SQLiteStore
from harness.models import PolicyLevel, SafetySmokeCheck, SafetySmokeResult, TaskStatus
from harness.policy import resolve_backend_effective_policy
from harness.security import sanitize_for_logging


def run_safety_smoke(project_root: Path, config: HarnessConfig, store: SQLiteStore) -> SafetySmokeResult:
    checks = [
        _sandbox_network_check(config),
        _backend_boundary_check(config),
        _artifact_evidence_check(store),
        _task_queue_non_execution_check(store),
        _manifest_policy_check(store),
    ]
    return SafetySmokeResult(ok=all(check.status == "pass" for check in checks), checks=checks)


def _sandbox_network_check(config: HarnessConfig) -> SafetySmokeCheck:
    ok = config.sandbox.network is False
    return SafetySmokeCheck(
        id="sandbox_network_disabled",
        status="pass" if ok else "fail",
        message="Sandbox network is disabled by default." if ok else "Sandbox network is enabled.",
        evidence={
            "network": config.sandbox.network,
            "workdir": config.sandbox.workdir,
            "timeout_seconds": config.sandbox.timeout_seconds,
        },
    )


def _backend_boundary_check(config: HarnessConfig) -> SafetySmokeCheck:
    evidence = []
    failures = []
    for name, backend in sorted(config.backends.items()):
        descriptor = backend.to_descriptor()
        policy = resolve_backend_effective_policy(descriptor)
        item = {
            "name": name,
            "billing_mode": descriptor.metadata.billing_mode.value,
            "execution_location": descriptor.metadata.execution_location.value,
            "data_boundary": descriptor.metadata.data_boundary.value,
            "allow_network": descriptor.metadata.allow_network,
            "levels": {key: value.value for key, value in policy.levels.items()},
            "constraints": descriptor.constraints,
        }
        evidence.append(item)
        if descriptor.metadata.billing_mode.value == "paid_api" and policy.levels["paid_provider"] != PolicyLevel.FORBIDDEN:
            failures.append(f"{name}: paid_provider is not forbidden")
        if descriptor.metadata.allow_network and policy.levels["external_network"] == PolicyLevel.ALLOWED:
            failures.append(f"{name}: external_network is allowed")
        if "settings" in json.dumps(descriptor.model_dump(mode="json")):
            failures.append(f"{name}: descriptor exposed backend settings")
    return SafetySmokeCheck(
        id="backend_boundaries",
        status="pass" if not failures else "fail",
        message="Backend descriptors preserve boundary policy." if not failures else "; ".join(failures),
        evidence={"backends": sanitize_for_logging(evidence)},
    )


def _artifact_evidence_check(store: SQLiteStore) -> SafetySmokeCheck:
    evidence = []
    failures = []
    for run in store.list_runs():
        for artifact in store.verify_artifacts(run.id):
            evidence.append(
                {
                    "run_id": run.id,
                    "artifact_id": artifact.id,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "evidence_status": artifact.evidence_status,
                }
            )
            if artifact.evidence_status in {"mismatch", "missing"}:
                failures.append(f"{artifact.id}: {artifact.evidence_status}")
    return SafetySmokeCheck(
        id="artifact_evidence",
        status="pass" if not failures else "fail",
        message="Artifact evidence is present and not drifted." if not failures else "; ".join(failures),
        evidence={"artifacts": evidence},
    )


def _task_queue_non_execution_check(store: SQLiteStore) -> SafetySmokeCheck:
    failures = []
    leased_attempts = []
    for attempt in store.list_task_attempts():
        if attempt.status == TaskStatus.LEASED:
            leased_attempts.append({"id": attempt.id, "task_id": attempt.task_id, "run_id": attempt.run_id})
            if attempt.run_id is not None:
                failures.append(f"{attempt.id}: leased queue attempt has run_id")
    return SafetySmokeCheck(
        id="task_queue_non_execution",
        status="pass" if not failures else "fail",
        message="Leased queue attempts remain non-executing." if not failures else "; ".join(failures),
        evidence={"leased_attempts": leased_attempts},
    )


def _manifest_policy_check(store: SQLiteStore) -> SafetySmokeCheck:
    failures = []
    evidence = []
    for run in store.list_runs():
        manifest = store.build_run_manifest(run.id)
        evidence.append(
            {
                "run_id": run.id,
                "schema_version": manifest.schema_version,
                "effective_policy_sha256": manifest.effective_policy_sha256,
                "backend_descriptor_sha256": manifest.backend_descriptor_sha256,
            }
        )
        if manifest.schema_version != "harness.manifest/v1.1":
            failures.append(f"{run.id}: manifest is not v1.1")
        if manifest.effective_policy is None or not manifest.effective_policy_sha256:
            failures.append(f"{run.id}: missing effective policy evidence")
    return SafetySmokeCheck(
        id="manifest_policy_evidence",
        status="pass" if not failures else "fail",
        message="Run manifests include runtime policy evidence." if not failures else "; ".join(failures),
        evidence={"runs": evidence},
    )
