import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.evals import run_safety_smoke, run_security_check, run_security_layer_audit
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.integrity import check_builtin_spec_integrity, run_integrity_check
from harness.memory.sqlite_store import SQLiteStore
from harness.registry import builtin_spec_registry
from harness.specs import ToolPermission, ToolPolicy
from harness.traces import export_run_trace, to_otel_json

runner = CliRunner()


def test_safety_smoke_passes_on_clean_initialized_project(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="trace run", task_type="phase_1a_test")
    store.append_event(run.id, "info", "unit_event", "Unit event.", {"value": 1})

    result = run_safety_smoke(tmp_path, default_config(), store)

    assert result.schema_version == "harness.evals.safety_smoke/v1"
    assert result.ok is True
    assert {check.id for check in result.checks} == {
        "sandbox_network_disabled",
        "backend_boundaries",
        "artifact_evidence",
        "task_queue_non_execution",
        "manifest_policy_evidence",
    }
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_safety_smoke_fails_on_artifact_drift(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="drift run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "evidence.txt"
    artifact_path.write_text("initial", encoding="utf-8")
    artifact = store.register_artifact(run.id, "evidence", artifact_path)
    artifact_path.write_text("changed", encoding="utf-8")

    result = run_safety_smoke(tmp_path, default_config(), store)
    artifact_check = next(check for check in result.checks if check.id == "artifact_evidence")

    assert result.ok is False
    assert artifact_check.status == "fail"
    assert artifact.id in artifact_check.message
    assert "changed" not in json.dumps(result.model_dump(mode="json"))


def test_trace_export_links_run_event_artifact_policy_and_backend_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(
        goal="trace run",
        task_type="read_only_repo_summary",
        backend=default_config().backends["local_openai_compatible"],
    )
    store.append_event(run.id, "info", "trace_event", "Trace event.", {"secret": "OPENAI_API_KEY=sk-secret"})
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "trace.txt"
    artifact_path.write_text("trace artifact body", encoding="utf-8")
    artifact = store.register_artifact(run.id, "trace_artifact", artifact_path)

    export = export_run_trace(tmp_path, store, run.id)
    payload = to_otel_json(export)
    serialized = json.dumps(payload)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}

    assert payload["schema_version"] == "harness.trace_export/v1"
    assert payload["ok"] is True
    assert payload["format"] == "otel-json"
    assert payload["run_id"] == run.id
    assert len(payload["trace_id"]) == 32
    assert "harness.run" in span_names
    assert "harness.policy" in span_names
    assert "harness.backend" in span_names
    assert "harness.sandbox" in span_names
    assert "harness.event.trace_event" in span_names
    assert "harness.artifact.trace_artifact" in span_names
    assert "harness.context.run_goal" in span_names
    assert "harness.context.artifact" in span_names
    sandbox_span = next(span for span in spans if span["name"] == "harness.sandbox")
    sandbox_attributes = {attribute["key"]: attribute["value"] for attribute in sandbox_span["attributes"]}
    assert sandbox_attributes["sandbox.profile_id"] == "read_only_codex"
    assert sandbox_attributes["sandbox.tier"] == "read_only"
    run_span = next(span for span in spans if span["name"] == "harness.run")
    run_attributes = {attribute["key"]: attribute["value"] for attribute in run_span["attributes"]}
    assert "artifact_content_not_authority" in run_attributes["context.warning_codes"]
    assert run_attributes["trace.provenance_id"].startswith("artprov_")
    assert run_attributes["trace.producer"] == "harness.trace_export"
    context_span = next(span for span in spans if span["name"] == "harness.context.artifact")
    context_attributes = {attribute["key"]: attribute["value"] for attribute in context_span["attributes"]}
    assert context_attributes["context.trust_level"] == "artifact"
    assert context_attributes["context.redaction_state"] == "not_required"
    assert artifact.sha256 in serialized
    assert "trace artifact body" not in serialized
    assert "sk-secret" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "api_key" not in serialized
    assert "base_url" not in serialized


def test_integrity_check_passes_uninitialized_project_and_cli_alias(tmp_path) -> None:
    direct = run_integrity_check(tmp_path)
    evals = runner.invoke(app, ["evals", "run", "--suite", "integrity", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["integrity", "check", "--project", str(tmp_path), "--output", "json"])

    assert direct.schema_version == "harness.integrity_check_result/v1"
    assert direct.ok is True
    assert direct.summary["fail"] == 0
    assert any(check.subject_kind.value == "builtin_spec" for check in direct.checks)
    assert any(check.subject_kind.value == "adapter_descriptor" for check in direct.checks)
    assert any(check.subject_kind.value == "tui_static_asset" for check in direct.checks)
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output)["schema_version"] == "harness.integrity_check_result/v1"
    assert json.loads(alias.output)["schema_version"] == "harness.integrity_check_result/v1"
    assert not (tmp_path / ".harness").exists()


def test_integrity_check_fails_closed_on_policy_broadening(monkeypatch) -> None:
    registry = builtin_spec_registry()
    policy_id = next(iter(registry.tool_policies))
    broadened = ToolPolicy.model_construct(
        tools={},
        network=ToolPermission.ALLOWED,
        active_repo_write=ToolPermission.FORBIDDEN,
        hosted_boundary=ToolPermission.APPROVAL_REQUIRED,
    )
    registry = registry.model_copy(update={"tool_policies": {**registry.tool_policies, policy_id: broadened}})
    monkeypatch.setattr("harness.integrity.builtin_spec_registry", lambda: registry)

    checks = check_builtin_spec_integrity()
    invariant = next(check for check in checks if check.subject_id == "registry_security_invariants")

    assert invariant.status.value == "fail"
    assert invariant.subject_kind.value == "builtin_spec"


def test_security_layer_audit_passes_clean_initialized_project_with_dry_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    result = execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner)
    assert result.ok is True

    audit = run_security_layer_audit(tmp_path)
    evals = runner.invoke(app, ["evals", "run", "--suite", "security-layer", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["security", "audit", "--project", str(tmp_path), "--output", "json"])

    assert audit.schema_version == "harness.security_layer_audit/v1"
    assert audit.ok is True
    assert audit.summary["fail"] == 0
    assert {check.id for check in audit.checks} >= {
        "registered_adapters_have_sandbox_profiles",
        "runtime_manifest_evidence",
        "security_detections_callable",
        "integrity_checks_pass",
    }
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output)["schema_version"] == "harness.security_layer_audit/v1"
    assert json.loads(alias.output)["schema_version"] == "harness.security_layer_audit/v1"


def test_security_layer_audit_uninitialized_project_is_read_only(tmp_path) -> None:
    audit = run_security_layer_audit(tmp_path)
    result = runner.invoke(app, ["evals", "run", "--suite", "security-layer", "--project", str(tmp_path), "--output", "json"])

    assert audit.ok is True
    assert audit.summary["skipped"] >= 1
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".harness").exists()


def test_security_layer_audit_fails_if_adapter_profile_missing(tmp_path, monkeypatch) -> None:
    descriptors = list_execution_adapter_descriptors()
    broken = descriptors[0].model_copy(update={"sandbox_profile_id": None})
    monkeypatch.setattr("harness.evals.list_execution_adapter_descriptors", lambda: [broken, *descriptors[1:]])

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "registered_adapters_have_sandbox_profiles")

    assert audit.ok is False
    assert check.status == "fail"


def test_security_layer_audit_fails_if_registered_manifest_lacks_sandbox_or_provenance(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    assert execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner).ok is True
    original = SQLiteStore.build_run_manifest

    def broken_manifest(self, run_id):
        manifest = original(self, run_id)
        artifacts = [artifact.model_copy(update={"provenance": None}) for artifact in manifest.artifacts]
        return manifest.model_copy(update={"sandbox_profile": None, "artifacts": artifacts})

    monkeypatch.setattr(SQLiteStore, "build_run_manifest", broken_manifest)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "runtime_manifest_evidence")

    assert audit.ok is False
    assert check.status == "fail"


def test_security_check_passes_clean_project_and_cli_alias_matches(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="trace run", task_type="phase_1a_test")
    paths = store.initialize_run_artifacts(run.id)
    store.register_artifact(run.id, "events", paths["events"])

    direct = run_security_check(tmp_path, store)
    evals = runner.invoke(app, ["evals", "run", "--suite", "security", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["security", "check", "--project", str(tmp_path), "--output", "json"])

    assert direct.schema_version == "harness.security_check/v1"
    assert direct.ok is True
    assert direct.findings == []
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output) == json.loads(alias.output)


def test_security_check_detects_synthetic_unsafe_metadata_without_echoing_secrets(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    daemon = store.ensure_daemon(owner="test")
    store.record_daemon_event(
        daemon.id,
        "execution_adapter_rejected",
        "Unknown adapter rejected.",
        {
            "adapter_id": "unknown_adapter",
            "reason_code": "unknown_adapter",
            "rejection_reasons": ["OPENAI_API_KEY=sk-secret"],
        },
    )
    store.record_daemon_event(
        daemon.id,
        "execution_adapter_rejected",
        "Breaker open.",
        {"adapter_id": "dry_run", "reason_code": "breaker_open"},
    )
    hosted = store.create_run(
        goal="hosted without approval",
        task_type="repo_planning",
        backend=default_config().backends["codex_cli"],
    )
    store.append_event(hosted.id, "info", "apply_back_applied", "Applied.", {"files": ["app.py"]})
    store.append_event(hosted.id, "info", "docker_test", "Docker test.", {"network": True})
    artifact_path = tmp_path / ".harness" / "runs" / hosted.id / "metadata.txt"
    artifact_path.write_text("safe body", encoding="utf-8")
    store.register_artifact(
        hosted.id,
        "metadata",
        artifact_path,
        metadata={"token": "Bearer abcdefghijklmnop"},
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET metadata_json = ? WHERE run_id = ? AND kind = ?",
            (json.dumps({"token": "Bearer abcdefghijklmnop"}), hosted.id, "metadata"),
        )

    result = run_security_check(tmp_path, store)
    serialized = json.dumps(result.model_dump(mode="json"))
    check_ids = {finding.check_id for finding in result.findings}

    assert result.ok is False
    assert {
        "unknown_adapter_dispatch_attempt",
        "breaker_open_execution_attempt",
        "hosted_boundary_without_approval",
        "apply_back_without_inspected_approval",
        "docker_network_enabled",
        "secret_like_metadata_output",
    } <= check_ids
    assert "sk-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "OPENAI_API_KEY" not in serialized
